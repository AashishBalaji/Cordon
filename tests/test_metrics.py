"""Tests for metrics.py -- the Prometheus exposition-format output."""

from cordon.budget import BudgetRegistry
from cordon.metrics import render_metrics
from cordon.slo import SLO
from cordon.telemetry import ERROR, SUCCESS, THROTTLED, TelemetryStore

SLO_DEF = SLO(name="availability", target=0.99, window_seconds=60.0)


def test_no_tenants_yields_metric_headers_but_no_series():
    body = render_metrics(TelemetryStore(), BudgetRegistry(), SLO_DEF).decode()
    assert "cordon_requests_total" in body
    assert 'tenant_id="' not in body  # no tenant has been seen yet


def test_requests_total_counts_by_outcome():
    telemetry = TelemetryStore()
    telemetry.record("acme", latency_ms=1.0, outcome=SUCCESS, now=0.0)
    telemetry.record("acme", latency_ms=1.0, outcome=SUCCESS, now=1.0)
    telemetry.record("acme", latency_ms=1.0, outcome=ERROR, now=2.0)
    telemetry.record("acme", latency_ms=0.0, outcome=THROTTLED, now=3.0)

    body = render_metrics(telemetry, BudgetRegistry(), SLO_DEF).decode()
    assert 'cordon_requests_total{outcome="success",tenant_id="acme"} 2.0' in body
    assert 'cordon_requests_total{outcome="error",tenant_id="acme"} 1.0' in body
    assert 'cordon_requests_total{outcome="throttled",tenant_id="acme"} 1.0' in body


def test_tenants_are_isolated_in_metrics_output():
    telemetry = TelemetryStore()
    telemetry.record("noisy", latency_ms=1.0, outcome=ERROR, now=0.0)
    telemetry.record("quiet", latency_ms=1.0, outcome=SUCCESS, now=0.0)

    body = render_metrics(telemetry, BudgetRegistry(), SLO_DEF).decode()
    assert 'cordon_requests_total{outcome="error",tenant_id="noisy"} 1.0' in body
    assert 'cordon_requests_total{outcome="error",tenant_id="quiet"} 0.0' in body


def test_headroom_reported_per_tenant_per_budget():
    budgets = BudgetRegistry()
    telemetry = TelemetryStore()
    telemetry.record("acme", latency_ms=1.0, outcome=SUCCESS, now=0.0)
    budgets.charge("acme", request_cost=10.0, now=0.0)

    body = render_metrics(telemetry, budgets, SLO_DEF).decode()
    assert 'cordon_budget_headroom_ratio{budget="request_rate",tenant_id="acme"}' in body
    assert 'cordon_budget_headroom_ratio{budget="resource_cost",tenant_id="acme"}' in body


def test_burn_rate_and_should_page_present_per_tenant():
    # No explicit `now` here (unlike the other tests): compute_burn_rate
    # inside collect() reads the real clock with no way for this test to
    # inject a fake one, so the events must be recorded against that same
    # real clock to land inside its window.
    telemetry = TelemetryStore()
    for _ in range(20):
        telemetry.record("acme", latency_ms=1.0, outcome=ERROR)

    body = render_metrics(telemetry, BudgetRegistry(), SLO_DEF).decode()
    assert 'cordon_slo_burn_rate{tenant_id="acme"}' in body
    assert 'cordon_slo_should_page{tenant_id="acme"} 1.0' in body


def test_multi_window_should_page_gauge_present_per_tenant():
    # Same real-clock caveat as the single-window test above: enough
    # dense, sustained errors that both the fast-burn and slow-burn
    # rules' short windows clear MIN_REQUESTS_FOR_SIGNAL.
    telemetry = TelemetryStore()
    for _ in range(40):
        telemetry.record("acme", latency_ms=1.0, outcome=ERROR)

    body = render_metrics(telemetry, BudgetRegistry(), SLO_DEF).decode()
    assert 'cordon_slo_should_page_multi_window{tenant_id="acme"}' in body
