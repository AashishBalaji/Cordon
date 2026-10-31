"""Tests for slo.py -- burn-rate computation, the layer that decides
whether telemetry is worth paging a human for."""

import pytest

from cordon.slo import (
    SLO,
    DEFAULT_MULTI_WINDOW_RULES,
    MultiWindowSLO,
    check_all_tenants_multi_window,
    compute_burn_rate,
    evaluate_multi_window,
    throttled_fraction,
)
from cordon.telemetry import ERROR, SUCCESS, THROTTLED, TelemetryStore


def _slo(target=0.99, window=60.0):
    return SLO(name="availability", target=target, window_seconds=window)


def test_no_requests_means_zero_burn_rate_not_a_page():
    store = TelemetryStore()
    result = compute_burn_rate(store, "t1", _slo(), now=0.0)
    assert result.burn_rate == 0.0
    assert result.should_page is False


def test_low_request_count_never_pages_even_with_100_percent_errors():
    # Classic alert-fatigue trap: 1 error out of 1 request looks like a
    # 100% error rate but is pure noise, not a signal.
    store = TelemetryStore()
    store.record("t1", latency_ms=1.0, outcome=ERROR, now=0.0)
    result = compute_burn_rate(store, "t1", _slo(target=0.99), now=0.0)
    assert result.observed_error_rate == 1.0
    assert result.should_page is False  # below MIN_REQUESTS_FOR_SIGNAL


def test_healthy_tenant_well_within_budget_does_not_page():
    store = TelemetryStore()
    for i in range(20):
        store.record("t1", latency_ms=5.0, outcome=SUCCESS, now=float(i))
    result = compute_burn_rate(store, "t1", _slo(target=0.99, window=60.0), now=20.0)
    assert result.error_count == 0
    assert result.burn_rate == 0.0
    assert result.should_page is False


def test_fast_burn_with_enough_volume_pages():
    store = TelemetryStore()
    # 99% target -> 1% error budget. 50% of 20 requests failing is a
    # burn rate of 0.50 / 0.01 = 50x -- well above the page threshold.
    for i in range(20):
        outcome = ERROR if i % 2 == 0 else SUCCESS
        store.record("t1", latency_ms=5.0, outcome=outcome, now=float(i))
    result = compute_burn_rate(store, "t1", _slo(target=0.99, window=60.0), now=20.0)
    assert result.burn_rate == pytest.approx(50.0)
    assert result.should_page is True


def test_throttled_requests_never_count_as_slo_errors():
    # A tenant correctly contained by its own budget should not page the
    # SLO -- that would mean the isolation mechanism paging on its own
    # success.
    store = TelemetryStore()
    for i in range(20):
        store.record("t1", latency_ms=0.0, outcome=THROTTLED, now=float(i))
    result = compute_burn_rate(store, "t1", _slo(target=0.99, window=60.0), now=20.0)
    assert result.error_count == 0
    assert result.should_page is False


def test_throttled_fraction_separate_from_burn_rate():
    store = TelemetryStore()
    for i in range(10):
        store.record("t1", latency_ms=0.0, outcome=THROTTLED, now=float(i))
    frac = throttled_fraction(store, "t1", window_seconds=60.0, now=10.0)
    assert frac == 1.0


def test_tenants_are_isolated_in_burn_rate_too():
    store = TelemetryStore()
    for i in range(20):
        store.record("noisy", latency_ms=1.0, outcome=ERROR, now=float(i))
    for i in range(20):
        store.record("quiet", latency_ms=1.0, outcome=SUCCESS, now=float(i))
    noisy = compute_burn_rate(store, "noisy", _slo(target=0.99), now=20.0)
    quiet = compute_burn_rate(store, "quiet", _slo(target=0.99), now=20.0)
    assert noisy.should_page is True
    assert quiet.should_page is False


# -- Multi-window, multi-burn-rate rules (slo.py's upgrade over the
# single-window primitive above) --------------------------------------

def test_default_rules_are_fast_and_slow_burn_at_googles_ratios():
    names = [r.name for r in DEFAULT_MULTI_WINDOW_RULES]
    assert names == ["fast_burn", "slow_burn"]
    for rule in DEFAULT_MULTI_WINDOW_RULES:
        # Same 12:1 long:short ratio Google's SRE workbook uses.
        assert rule.long_window_seconds == rule.short_window_seconds * 12


def test_multi_window_no_requests_never_pages():
    store = TelemetryStore()
    mw_slo = MultiWindowSLO(slo=_slo(target=0.99))
    result = evaluate_multi_window(store, "t1", mw_slo, now=0.0)
    assert result.should_page is False
    assert all(not r.triggered for r in result.rule_results)


def test_multi_window_pages_only_when_both_long_and_short_window_agree():
    # Sustained 50% errors, dense enough to clear MIN_REQUESTS_FOR_SIGNAL
    # in every rule's short window too, across the whole 300s span.
    store = TelemetryStore()
    for i in range(300):
        outcome = ERROR if i % 2 == 0 else SUCCESS
        store.record("t1", latency_ms=1.0, outcome=outcome, now=float(i))
    mw_slo = MultiWindowSLO(slo=_slo(target=0.99))
    result = evaluate_multi_window(store, "t1", mw_slo, now=299.0)
    assert result.should_page is True
    slow_burn = next(r for r in result.rule_results if r.rule_name == "slow_burn")
    assert slow_burn.triggered is True
    assert slow_burn.long_window_burn_rate.should_page is True
    assert slow_burn.short_window_burn_rate.should_page is True


def test_short_window_recovery_stops_paging_despite_contaminated_long_window():
    # An incident that already ended: a burst of errors early on, then
    # clean traffic right up to "now". The long window still sees the
    # old errors and looks unhealthy on its own; the short window is
    # clean. Multi-window requires BOTH to agree, so this must not page
    # -- the whole point of pairing the windows is fast recovery.
    store = TelemetryStore()
    for i in range(300):
        outcome = ERROR if i < 50 else SUCCESS
        store.record("t1", latency_ms=1.0, outcome=outcome, now=float(i))
    mw_slo = MultiWindowSLO(slo=_slo(target=0.99))
    result = evaluate_multi_window(store, "t1", mw_slo, now=299.0)
    slow_burn = next(r for r in result.rule_results if r.rule_name == "slow_burn")
    assert slow_burn.long_window_burn_rate.should_page is True  # still "contaminated"
    assert slow_burn.short_window_burn_rate.should_page is False  # already recovered
    assert slow_burn.triggered is False
    assert result.should_page is False


def test_multi_window_tenants_are_isolated():
    store = TelemetryStore()
    for i in range(300):
        store.record("noisy", latency_ms=1.0, outcome=ERROR, now=float(i))
    for i in range(300):
        store.record("quiet", latency_ms=1.0, outcome=SUCCESS, now=float(i))
    mw_slo = MultiWindowSLO(slo=_slo(target=0.99))
    results = {
        r.tenant_id: r for r in check_all_tenants_multi_window(store, mw_slo, now=299.0)
    }
    assert results["noisy"].should_page is True
    assert results["quiet"].should_page is False
