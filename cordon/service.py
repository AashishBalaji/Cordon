"""
service.py -- the case-management logic itself, with every
operation routed through a tenant's budget before it touches
storage, and every outcome recorded to telemetry regardless of
whether it succeeded, failed, or was throttled.

This is the one place budget.py, telemetry.py, and the in-memory
store meet. Deliberately not a web framework concern -- api.py is a
thin HTTP wrapper around this, so these rules are testable directly
with no server running.
"""

from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional

from .budget import BudgetExceeded, BudgetRegistry
from .models import Case, estimate_request_cost
from .telemetry import ERROR, SUCCESS, THROTTLED, TelemetryStore


class CaseNotFound(Exception):
    pass


class CaseService:
    def __init__(
        self,
        budgets: Optional[BudgetRegistry] = None,
        telemetry: Optional[TelemetryStore] = None,
    ):
        self.budgets = budgets or BudgetRegistry()
        self.telemetry = telemetry or TelemetryStore()
        self._cases: Dict[str, Case] = {}

    def _run_charged(self, tenant_id: str, operation: str, payload_size: int, fn):
        """Charge the tenant's budget, time the call, record the
        outcome to telemetry, and re-raise BudgetExceeded /
        unexpected errors after recording them -- the caller (api.py)
        decides what HTTP status each outcome becomes."""
        cost = estimate_request_cost(operation, payload_size)
        start = time.monotonic()
        try:
            self.budgets.charge(tenant_id, cost)
        except BudgetExceeded:
            self.telemetry.record(tenant_id, latency_ms=0.0, outcome=THROTTLED)
            raise
        try:
            result = fn()
        except Exception:
            latency_ms = (time.monotonic() - start) * 1000.0
            self.telemetry.record(tenant_id, latency_ms=latency_ms, outcome=ERROR)
            raise
        latency_ms = (time.monotonic() - start) * 1000.0
        self.telemetry.record(tenant_id, latency_ms=latency_ms, outcome=SUCCESS)
        return result

    def create_case(self, tenant_id: str, subject: str, description: str = "") -> Case:
        payload_size = len(subject) + len(description)

        def _do():
            case = Case(
                case_id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                subject=subject,
                description=description,
            )
            self._cases[case.case_id] = case
            return case

        return self._run_charged(tenant_id, "write", payload_size, _do)

    def get_case(self, tenant_id: str, case_id: str) -> Case:
        def _do():
            case = self._cases.get(case_id)
            # Cross-tenant lookups fail the same way a missing case
            # does -- a tenant never learns whether another tenant's
            # case_id exists at all.
            if case is None or case.tenant_id != tenant_id:
                raise CaseNotFound(case_id)
            return case

        return self._run_charged(tenant_id, "read", 0, _do)

    def list_cases(self, tenant_id: str) -> List[Case]:
        def _do():
            return [c for c in self._cases.values() if c.tenant_id == tenant_id]

        return self._run_charged(tenant_id, "list", 0, _do)

    def close_case(self, tenant_id: str, case_id: str) -> Case:
        def _do():
            case = self._cases.get(case_id)
            if case is None or case.tenant_id != tenant_id:
                raise CaseNotFound(case_id)
            case.status = "closed"
            return case

        return self._run_charged(tenant_id, "write", 0, _do)
