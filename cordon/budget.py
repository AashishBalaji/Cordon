"""
budget.py -- per-tenant resource budgets, the actual isolation
mechanism this whole project is built around.

The real-world analogue is Salesforce's own Apex "governor limits":
on a multi-tenant platform, a single tenant's runaway query or loop
can't be allowed to consume resources another tenant is paying for,
so every transaction is capped against a hard, per-tenant budget
rather than a single shared one. This module is a small, honest
version of that idea -- deliberately simple enough to read in one
sitting, not a reimplementation of Apex's actual limit set.

Two budgets are tracked independently per tenant, each its own
token bucket so one kind of abuse (a request flood) can't silently
borrow room from the other (a few very expensive requests):

  - request-rate budget: requests per rolling window, a classic
    token bucket (refills continuously, capped at a burst size).
  - cost budget: a per-request "resource cost" (standing in for
    Apex's CPU-time/SOQL-count/heap limits combined into one
    number) drawn from a second bucket on the same rolling window.

Nothing here calls a model or guesses at intent -- it's arithmetic
against two counters, same as everywhere else in this author's
projects where a decision can be made by counting instead of asking.

Concurrency: every mutating operation on a given tenant's budget
(`TenantBudget.charge`, `.headroom`) holds that tenant's own lock for
its whole check-then-take sequence, and `BudgetRegistry.get` holds a
short registry-wide lock only around first-creation of a tenant's
buckets. Two different tenants never contend on the same lock, so
isolation holds under real concurrency, not just sequential calls --
see `tests/test_concurrency.py` and `scripts/concurrent_load_test.py`
for what this is actually defending against: a check-then-take race
(read `available()`, then `take()`) that, without a lock, can let two
threads both pass the check against the same tokens and overdraw the
bucket below zero.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


class BudgetExceeded(Exception):
    """Raised when a tenant has no room left in one of its budgets.
    Carries which budget and by how much, so the caller (or the test
    that provoked this) can tell a rate problem from a cost problem."""

    def __init__(self, tenant_id: str, budget_name: str, requested: float, available: float):
        self.tenant_id = tenant_id
        self.budget_name = budget_name
        self.requested = requested
        self.available = available
        super().__init__(
            f"tenant {tenant_id!r} exceeded its {budget_name} budget: "
            f"requested {requested:.2f}, {available:.2f} available"
        )


@dataclass
class TokenBucket:
    """A standard token bucket: refills continuously up to `capacity`
    at `refill_rate` tokens/second, and `take(amount)` either
    succeeds (deducting `amount`) or fails leaving the bucket
    untouched -- a failed take never partially drains the bucket."""

    capacity: float
    refill_rate: float
    tokens: float = field(init=False)
    _last_refill: Optional[float] = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        # Deliberately not stamped with a real clock reading here: the
        # first call to _refill() (whatever clock -- real or a test's
        # fake "now") establishes the baseline, so a caller passing
        # explicit `now` values (as every test in this project does)
        # never has them compared against a real wall-clock timestamp
        # they have no way to match.
        self._last_refill = None

    def _refill(self, now: float) -> None:
        if self._last_refill is None:
            self._last_refill = now
            return
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self._last_refill = now

    def available(self, now: float | None = None) -> float:
        now = now if now is not None else time.monotonic()
        self._refill(now)
        return self.tokens

    def take(self, amount: float, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        self._refill(now)
        if amount > self.tokens:
            return False
        self.tokens -= amount
        return True


@dataclass
class TenantBudget:
    """Both buckets for one tenant, isolated from every other
    tenant's -- this is the whole isolation guarantee in this
    project: tenants never share a bucket, so there is no bucket
    for a noisy tenant to drain that anyone else depends on."""

    tenant_id: str
    rate_bucket: TokenBucket
    cost_bucket: TokenBucket
    # Not compared/printed: a Lock has no meaningful equality or repr,
    # and including it in either would just be noise.
    _lock: threading.Lock = field(
        init=False, default_factory=threading.Lock, repr=False, compare=False
    )

    def charge(self, request_cost: float, now: float | None = None) -> None:
        """Deduct one request (rate budget) and its resource cost
        (cost budget). Checks both before taking either, so a
        request that would exceed the cost budget doesn't still
        consume a rate-budget token on its way to being rejected.

        The whole check-then-take sequence holds this tenant's lock:
        without it, two real threads can both read `available() >=
        amount` as true for the same tokens before either calls
        `take()`, and both succeed -- a classic check-then-act race
        that silently overdraws the bucket below zero. One tenant's
        lock never blocks another tenant's charge() call."""
        with self._lock:
            now = now if now is not None else time.monotonic()
            rate_available = self.rate_bucket.available(now)
            if rate_available < 1.0:
                raise BudgetExceeded(self.tenant_id, "request_rate", 1.0, rate_available)
            if self.cost_bucket.available(now) < request_cost:
                raise BudgetExceeded(
                    self.tenant_id, "resource_cost", request_cost,
                    self.cost_bucket.available(now),
                )
            self.rate_bucket.take(1.0, now)
            self.cost_bucket.take(request_cost, now)

    def headroom(self, now: float | None = None) -> Dict[str, float]:
        """Fraction of each budget still available, 0.0-1.0 -- what
        slo.py watches to catch a tenant trending toward its limit
        before it actually hits one. Shares charge()'s lock: `available()`
        mutates the bucket's refill state, so a concurrent charge() and
        headroom() call on the same tenant could otherwise interleave
        mid-refill and read a torn value."""
        with self._lock:
            now = now if now is not None else time.monotonic()
            return {
                "request_rate": self.rate_bucket.available(now) / self.rate_bucket.capacity,
                "resource_cost": self.cost_bucket.available(now) / self.cost_bucket.capacity,
            }


# Defaults, stated as named constants rather than buried magic
# numbers. Deliberately generous enough that a well-behaved tenant
# never notices them -- the point is to catch runaway behavior, not
# to meter ordinary use.
DEFAULT_REQUEST_RATE_CAPACITY = 50.0
DEFAULT_REQUEST_RATE_PER_SECOND = 10.0
DEFAULT_COST_CAPACITY = 500.0
DEFAULT_COST_REFILL_PER_SECOND = 100.0


class BudgetRegistry:
    """Owns one TenantBudget per tenant, creating it on first use
    with the default caps. This is the one shared object in the
    service -- but it only ever hands out per-tenant buckets, never
    a bucket two tenants both draw from."""

    def __init__(
        self,
        rate_capacity: float = DEFAULT_REQUEST_RATE_CAPACITY,
        rate_refill: float = DEFAULT_REQUEST_RATE_PER_SECOND,
        cost_capacity: float = DEFAULT_COST_CAPACITY,
        cost_refill: float = DEFAULT_COST_REFILL_PER_SECOND,
    ) -> None:
        self._rate_capacity = rate_capacity
        self._rate_refill = rate_refill
        self._cost_capacity = cost_capacity
        self._cost_refill = cost_refill
        self._tenants: Dict[str, TenantBudget] = {}
        # Only guards first-creation of a tenant's buckets below, never
        # charge()/headroom() themselves -- those hold the per-tenant
        # lock instead, so two tenants never contend on this one.
        self._creation_lock = threading.Lock()

    def get(self, tenant_id: str) -> TenantBudget:
        existing = self._tenants.get(tenant_id)
        if existing is not None:
            return existing
        with self._creation_lock:
            # Double-checked: another thread may have created this
            # tenant's buckets while we were waiting for the lock.
            if tenant_id not in self._tenants:
                self._tenants[tenant_id] = TenantBudget(
                    tenant_id=tenant_id,
                    rate_bucket=TokenBucket(self._rate_capacity, self._rate_refill),
                    cost_bucket=TokenBucket(self._cost_capacity, self._cost_refill),
                )
            return self._tenants[tenant_id]

    def charge(self, tenant_id: str, request_cost: float, now: float | None = None) -> None:
        self.get(tenant_id).charge(request_cost, now)

    def headroom(self, tenant_id: str, now: float | None = None) -> Dict[str, float]:
        return self.get(tenant_id).headroom(now)
