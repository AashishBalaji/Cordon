"""Tests for telemetry.py."""

from cordon.telemetry import ERROR, SUCCESS, THROTTLED, TelemetryStore


def test_record_and_read_back_within_window():
    store = TelemetryStore()
    store.record("t1", latency_ms=12.0, outcome=SUCCESS, now=0.0)
    store.record("t1", latency_ms=20.0, outcome=ERROR, now=1.0)
    events = store.events_in_window("t1", window_seconds=10.0, now=5.0)
    assert len(events) == 2
    assert {e.outcome for e in events} == {SUCCESS, ERROR}


def test_events_outside_window_excluded():
    store = TelemetryStore()
    store.record("t1", latency_ms=1.0, outcome=SUCCESS, now=0.0)
    store.record("t1", latency_ms=1.0, outcome=SUCCESS, now=100.0)
    events = store.events_in_window("t1", window_seconds=10.0, now=105.0)
    assert len(events) == 1
    assert events[0].timestamp == 100.0


def test_tenants_are_isolated_in_telemetry_too():
    store = TelemetryStore()
    store.record("noisy", latency_ms=1.0, outcome=ERROR, now=0.0)
    quiet_events = store.events_in_window("quiet", window_seconds=10.0, now=0.0)
    assert quiet_events == []


def test_retention_prunes_old_events_on_read():
    store = TelemetryStore(retention_seconds=5.0)
    store.record("t1", latency_ms=1.0, outcome=SUCCESS, now=0.0)
    # Reading far in the future, past retention, should prune the old event
    store.events_in_window("t1", window_seconds=1000.0, now=100.0)
    assert store.events_in_window("t1", window_seconds=1000.0, now=100.0) == []


def test_throttled_outcome_recorded_distinctly():
    store = TelemetryStore()
    store.record("t1", latency_ms=0.0, outcome=THROTTLED, now=0.0)
    events = store.events_in_window("t1", window_seconds=10.0, now=0.0)
    assert events[0].outcome == THROTTLED
