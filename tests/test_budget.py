"""Tests for budget.py -- the actual isolation mechanism."""

import pytest

from cordon.budget import BudgetExceeded, BudgetRegistry, TenantBudget, TokenBucket


def test_token_bucket_starts_full():
    b = TokenBucket(capacity=10.0, refill_rate=1.0)
    assert b.available(now=0.0) == 10.0


def test_token_bucket_take_deducts():
    b = TokenBucket(capacity=10.0, refill_rate=1.0)
    assert b.take(4.0, now=0.0) is True
    assert b.available(now=0.0) == 6.0


def test_token_bucket_refuses_over_capacity_and_leaves_bucket_untouched():
    b = TokenBucket(capacity=10.0, refill_rate=1.0)
    assert b.take(11.0, now=0.0) is False
    assert b.available(now=0.0) == 10.0  # untouched, not partially drained


def test_token_bucket_refills_over_time():
    b = TokenBucket(capacity=10.0, refill_rate=2.0)
    b.take(10.0, now=0.0)
    assert b.available(now=0.0) == 0.0
    assert b.available(now=3.0) == 6.0  # 3s * 2/s = 6, capped at capacity
    assert b.available(now=100.0) == 10.0  # capped, never exceeds capacity


def test_tenant_budget_charges_both_buckets_on_success():
    budget = TenantBudget(
        tenant_id="t1", rate_bucket=TokenBucket(5.0, 1.0), cost_bucket=TokenBucket(100.0, 10.0)
    )
    budget.charge(request_cost=20.0, now=0.0)
    assert budget.rate_bucket.available(now=0.0) == 4.0
    assert budget.cost_bucket.available(now=0.0) == 80.0


def test_tenant_budget_rejects_on_rate_exhaustion_without_touching_cost_bucket():
    budget = TenantBudget(
        tenant_id="t1", rate_bucket=TokenBucket(1.0, 0.0), cost_bucket=TokenBucket(100.0, 0.0)
    )
    budget.charge(request_cost=10.0, now=0.0)  # uses the one rate token
    with pytest.raises(BudgetExceeded) as exc_info:
        budget.charge(request_cost=10.0, now=0.0)
    assert exc_info.value.budget_name == "request_rate"
    # cost bucket must be untouched by the rejected request
    assert budget.cost_bucket.available(now=0.0) == 90.0


def test_tenant_budget_rejects_on_cost_exhaustion_without_touching_rate_bucket():
    budget = TenantBudget(
        tenant_id="t1", rate_bucket=TokenBucket(100.0, 0.0), cost_bucket=TokenBucket(5.0, 0.0)
    )
    with pytest.raises(BudgetExceeded) as exc_info:
        budget.charge(request_cost=10.0, now=0.0)
    assert exc_info.value.budget_name == "resource_cost"
    assert budget.rate_bucket.available(now=0.0) == 100.0  # untouched


def test_registry_gives_each_tenant_its_own_isolated_buckets():
    registry = BudgetRegistry(
        rate_capacity=1.0, rate_refill=0.0, cost_capacity=100.0, cost_refill=0.0
    )
    registry.charge("noisy", request_cost=1.0, now=0.0)  # exhausts noisy's rate bucket

    with pytest.raises(BudgetExceeded):
        registry.charge("noisy", request_cost=1.0, now=0.0)

    # a second tenant is completely unaffected -- this is the isolation
    # guarantee under test, not an implementation detail
    registry.charge("quiet", request_cost=1.0, now=0.0)
    # "quiet"'s own bucket, drained by its own request -- not noisy's doing
    assert registry.headroom("quiet", now=0.0)["request_rate"] == 0.0
    assert registry.headroom("noisy", now=0.0)["request_rate"] == 0.0


def test_headroom_reports_fraction_remaining():
    registry = BudgetRegistry(
        rate_capacity=10.0, rate_refill=0.0, cost_capacity=100.0, cost_refill=0.0
    )
    registry.charge("t1", request_cost=25.0, now=0.0)
    headroom = registry.headroom("t1", now=0.0)
    assert headroom["request_rate"] == pytest.approx(0.9)  # 9/10 left
    assert headroom["resource_cost"] == pytest.approx(0.75)  # 75/100 left
