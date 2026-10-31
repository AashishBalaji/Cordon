"""Functional tests for CaseService -- the integration point between
budget.py and telemetry.py."""

import pytest

from cordon.budget import BudgetExceeded, BudgetRegistry
from cordon.service import CaseNotFound, CaseService
from cordon.telemetry import ERROR, SUCCESS, THROTTLED, TelemetryStore


def _tight_registry():
    """A registry with a deliberately tiny budget, so tests can
    exhaust it in a couple of calls rather than needing real time."""
    reg = BudgetRegistry(rate_capacity=100.0, rate_refill=0.0, cost_capacity=100.0, cost_refill=0.0)
    return reg


def test_create_and_get_case_round_trips():
    service = CaseService(budgets=_tight_registry(), telemetry=TelemetryStore())
    case = service.create_case("t1", "Login broken", "Can't log in since this morning")
    fetched = service.get_case("t1", case.case_id)
    assert fetched.case_id == case.case_id
    assert fetched.subject == "Login broken"


def test_a_tenant_cannot_see_another_tenants_case():
    service = CaseService(budgets=_tight_registry(), telemetry=TelemetryStore())
    case = service.create_case("t1", "Internal issue")
    with pytest.raises(CaseNotFound):
        service.get_case("t2", case.case_id)


def test_list_cases_is_scoped_per_tenant():
    service = CaseService(budgets=_tight_registry(), telemetry=TelemetryStore())
    service.create_case("t1", "A")
    service.create_case("t1", "B")
    service.create_case("t2", "C")
    assert len(service.list_cases("t1")) == 2
    assert len(service.list_cases("t2")) == 1


def test_close_case_updates_status():
    service = CaseService(budgets=_tight_registry(), telemetry=TelemetryStore())
    case = service.create_case("t1", "A")
    closed = service.close_case("t1", case.case_id)
    assert closed.status == "closed"


def test_exhausting_one_tenants_budget_never_touches_another_tenants():
    # The actual isolation guarantee, exercised end to end through the
    # service layer rather than just budget.py directly.
    registry = BudgetRegistry(
        rate_capacity=1.0, rate_refill=0.0, cost_capacity=1000.0, cost_refill=0.0
    )
    service = CaseService(budgets=registry, telemetry=TelemetryStore())

    service.create_case("noisy", "first")  # uses noisy's one rate token
    with pytest.raises(BudgetExceeded):
        service.create_case("noisy", "second")  # noisy is now throttled

    # a second tenant, sharing nothing with "noisy", is unaffected
    quiet_case = service.create_case("quiet", "unaffected")
    assert quiet_case.subject == "unaffected"


def test_throttled_request_recorded_as_throttled_not_error():
    registry = BudgetRegistry(
        rate_capacity=1.0, rate_refill=0.0, cost_capacity=1000.0, cost_refill=0.0
    )
    telemetry = TelemetryStore()
    service = CaseService(budgets=registry, telemetry=telemetry)

    service.create_case("t1", "first")
    with pytest.raises(BudgetExceeded):
        service.create_case("t1", "second")

    events = telemetry.events_in_window("t1", window_seconds=60.0, now=None)
    outcomes = [e.outcome for e in events]
    assert outcomes.count(SUCCESS) == 1
    assert outcomes.count(THROTTLED) == 1
    assert outcomes.count(ERROR) == 0


def test_get_missing_case_records_error_not_throttled():
    service = CaseService(budgets=_tight_registry(), telemetry=TelemetryStore())
    with pytest.raises(CaseNotFound):
        service.get_case("t1", "does-not-exist")
    events = service.telemetry.events_in_window("t1", window_seconds=60.0, now=None)
    assert events[-1].outcome == ERROR
