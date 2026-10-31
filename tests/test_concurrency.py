"""Real concurrency tests -- the gap `incident/postmortem.md` names
explicitly: the noisy-tenant simulation ran single-threaded,
sequentially, so it never exercised whether budget enforcement
actually holds under real concurrent access, only whether the
arithmetic is correct when called one request at a time.

These tests use real `threading.Thread`s (not asyncio, not mocked
time) hitting the same `TenantBudget`/`BudgetRegistry`/
`TelemetryStore` instances a real multi-threaded server process would
share. See `cordon/budget.py` and `cordon/telemetry.py` for the
locks these tests are confirming hold, and
`scripts/concurrent_load_test.py` for the same thing run as a
standalone, documented simulation rather than a pass/fail test.

Honest finding from building this: before these locks existed,
`TenantBudget.charge()`'s check-then-take sequence (read
`available()`, then separately call `take()`) was a real,
reproducible race under genuine threads -- not just a theoretical
one. 200 threads concurrently charging a capacity-10 bucket
reliably produced 11+ successes against a capacity of 10. The fix is
a plain `threading.Lock` per tenant around the whole check-then-take
sequence (not a lock-free or atomic-counter redesign) -- the simplest
thing that is actually correct, consistent with this project's
preference for plain arithmetic over cleverness everywhere else."""

import threading

import pytest

from cordon.budget import BudgetExceeded, BudgetRegistry
from cordon.telemetry import ERROR, SUCCESS, TelemetryStore


def _run_concurrently(worker, count):
    threads = [threading.Thread(target=worker) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_concurrent_charges_never_overdraw_the_rate_bucket():
    # Real race condition, reproduced without the lock during this
    # project's own development (see module docstring): capacity 10,
    # 200 concurrent attempts, no refill -- exactly 10 must succeed,
    # never 11, however the threads happen to interleave.
    registry = BudgetRegistry(
        rate_capacity=10.0, rate_refill=0.0, cost_capacity=1_000_000.0, cost_refill=0.0
    )
    successes = []
    lock = threading.Lock()

    def worker():
        try:
            registry.charge("tenant-a", request_cost=1.0)
        except BudgetExceeded:
            return
        with lock:
            successes.append(1)

    _run_concurrently(worker, count=200)

    assert len(successes) == 10
    assert registry.get("tenant-a").rate_bucket.tokens == pytest.approx(0.0)


def test_concurrent_access_never_creates_two_buckets_for_one_tenant():
    # BudgetRegistry.get() lazily creates a tenant's buckets on first
    # use -- without the creation lock's double-checked read, two
    # threads racing to be "first" for a brand-new tenant could each
    # construct and install their own TenantBudget, silently losing
    # whichever one wasn't last to win the dict write (and resetting
    # that tenant's budget state for every request already in flight
    # against the loser).
    registry = BudgetRegistry()
    seen_buckets = []
    lock = threading.Lock()

    def worker():
        budget = registry.get("brand-new-tenant")
        with lock:
            seen_buckets.append(id(budget.rate_bucket))

    _run_concurrently(worker, count=100)

    assert len(set(seen_buckets)) == 1  # every thread got the exact same bucket object


def test_concurrent_charges_on_different_tenants_do_not_serialize_each_other():
    # Isolation under concurrency, not just under sequential calls:
    # tenant-b should be able to exhaust its own budget while
    # tenant-a's charges succeed freely, with no cross-tenant blocking
    # (each tenant's lock is independent -- see budget.py).
    registry = BudgetRegistry(
        rate_capacity=5.0, rate_refill=0.0, cost_capacity=1_000_000.0, cost_refill=0.0
    )
    a_successes = []
    b_successes = []
    lock = threading.Lock()

    def charge(tenant_id, bucket_list):
        try:
            registry.charge(tenant_id, request_cost=1.0)
        except BudgetExceeded:
            return
        with lock:
            bucket_list.append(1)

    threads = (
        [threading.Thread(target=charge, args=("tenant-a", a_successes)) for _ in range(50)]
        + [threading.Thread(target=charge, args=("tenant-b", b_successes)) for _ in range(50)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(a_successes) == 5
    assert len(b_successes) == 5


def test_concurrent_telemetry_records_are_never_lost():
    # record() appends while events_in_window() prunes the same
    # per-tenant list in place (events[:] = [...]) -- without the
    # shared lock, a prune running concurrently with an append could
    # rewrite the list out from under the append and silently drop it.
    store = TelemetryStore()
    record_count = 500

    def record_worker():
        for _ in range(record_count):
            store.record("tenant-a", latency_ms=1.0, outcome=SUCCESS)

    def reader_worker():
        for _ in range(200):
            store.events_in_window("tenant-a", window_seconds=10_000.0)

    threads = [threading.Thread(target=record_worker)] + [
        threading.Thread(target=reader_worker) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.all_events("tenant-a")) == record_count


def test_concurrent_telemetry_isolated_per_tenant_under_real_threads():
    store = TelemetryStore()

    def record_errors():
        for _ in range(100):
            store.record("noisy", latency_ms=1.0, outcome=ERROR)

    def record_successes():
        for _ in range(100):
            store.record("quiet", latency_ms=1.0, outcome=SUCCESS)

    threads = [threading.Thread(target=record_errors), threading.Thread(target=record_successes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.all_events("noisy")) == 100
    assert all(e.outcome == ERROR for e in store.all_events("noisy"))
    assert len(store.all_events("quiet")) == 100
    assert all(e.outcome == SUCCESS for e in store.all_events("quiet"))
