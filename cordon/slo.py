"""
slo.py -- per-tenant SLOs and error-budget burn-rate, the layer
that turns raw telemetry into something worth paging a human for.

The standard problem with naive metric alerting ("alert if error
rate > 1%") is alert fatigue: thresholds fire on noise, get muted,
and then miss the real incident. The fix this module implements is
the well-known SRE "burn rate" technique: define how much error
budget an SLO allows over a window, measure how fast a tenant is
actually spending that budget right now, and only alert when the
*rate* of spending -- not the raw error count -- means the tenant
will blow through its whole budget too fast to be a shrug.

Scope, stated plainly: real multi-window multi-burn-rate alerting
(Google's SRE workbook) checks several window/threshold pairs at
once to catch both sudden spikes and slow leaks without flapping.
This implements one window with one threshold -- the core idea,
not the full production design -- and says so rather than
pretending otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .telemetry import ERROR, THROTTLED, TelemetryStore


@dataclass(frozen=True)
class SLO:
    """A tenant's availability target: `target` fraction of requests
    (e.g. 0.995) must succeed over `window_seconds`. `error_budget`
    is the remaining fraction allowed to fail (1 - target)."""

    name: str
    target: float
    window_seconds: float

    @property
    def error_budget(self) -> float:
        return 1.0 - self.target


@dataclass(frozen=True)
class BurnRateResult:
    tenant_id: str
    slo_name: str
    window_seconds: float
    request_count: int
    error_count: int
    observed_error_rate: float
    error_budget: float
    burn_rate: float  # error_rate / error_budget; 1.0 = burning exactly as fast as allowed
    should_page: bool


# A burn rate above this, sustained, means the tenant's monthly (or
# whatever window) error budget will be exhausted well before the
# window ends -- worth a human's attention. Below it is treated as
# normal variance, not an incident.
DEFAULT_PAGE_THRESHOLD = 10.0

# Below this many requests in the window, a burn rate is noise, not
# signal -- one failed request out of two looks like a 50% error
# rate and would otherwise page on nothing.
MIN_REQUESTS_FOR_SIGNAL = 10


def compute_burn_rate(
    telemetry: TelemetryStore,
    tenant_id: str,
    slo: SLO,
    now: float | None = None,
    page_threshold: float = DEFAULT_PAGE_THRESHOLD,
    min_requests: int = MIN_REQUESTS_FOR_SIGNAL,
) -> BurnRateResult:
    events = telemetry.events_in_window(tenant_id, slo.window_seconds, now)
    request_count = len(events)
    # Throttled requests are the isolation mechanism doing its job on
    # purpose, not the service failing the tenant -- counting them as
    # errors would make a correctly-contained noisy tenant page on its
    # own containment. Only genuine errors count against the SLO.
    error_count = sum(1 for e in events if e.outcome == ERROR)
    observed_error_rate = (error_count / request_count) if request_count else 0.0
    burn_rate = (observed_error_rate / slo.error_budget) if slo.error_budget > 0 else 0.0
    should_page = request_count >= min_requests and burn_rate >= page_threshold
    return BurnRateResult(
        tenant_id=tenant_id,
        slo_name=slo.name,
        window_seconds=slo.window_seconds,
        request_count=request_count,
        error_count=error_count,
        observed_error_rate=observed_error_rate,
        error_budget=slo.error_budget,
        burn_rate=burn_rate,
        should_page=should_page,
    )


def throttled_fraction(
    telemetry: TelemetryStore, tenant_id: str, window_seconds: float, now=None
) -> float:
    """Separate from burn rate on purpose: how much of a tenant's own
    traffic is being throttled right now. A tenant correctly
    contained by its own budget should show up here, not in the SLO
    burn rate -- this is what tells an on-call engineer "tenant X is
    noisy and being contained," distinct from "tenant X is actually
    broken."""
    events = telemetry.events_in_window(tenant_id, window_seconds, now)
    if not events:
        return 0.0
    return sum(1 for e in events if e.outcome == THROTTLED) / len(events)


def check_all_tenants(
    telemetry: TelemetryStore, slo: SLO, now: float | None = None
) -> List[BurnRateResult]:
    """Convenience for an on-call sweep: burn rate for every tenant
    telemetry has seen, so a genuinely affected tenant surfaces even
    if nobody thought to ask about it by name."""
    return [
        compute_burn_rate(telemetry, tenant_id, slo, now) for tenant_id in telemetry.known_tenants()
    ]
