"""Tests for slo.py -- burn-rate computation, the layer that decides
whether telemetry is worth paging a human for."""

import pytest

from cordon.slo import SLO, compute_burn_rate, throttled_fraction
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
