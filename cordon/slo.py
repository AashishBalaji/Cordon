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

`compute_burn_rate` below is the single-window primitive: one
window, one threshold. That alone has a real flapping problem --
a single long window is slow to both alert and recover (an incident
that ends still "looks" bad until the window fully rolls past it),
and a single short window pages on noise. Google's SRE workbook
fixes this with **multi-window, multi-burn-rate alerting**: pair a
long window (catches slow, sustained leaks) with a short window at
the same threshold (confirms the leak is still happening right now,
so recovery is fast), and only page when *both* agree. This module
implements that on top of the single-window primitive --
`MultiWindowSLO`, `BurnRateRule`, and `evaluate_multi_window` below
-- using the same long:short ratio (12:1) and the same two
thresholds (14.4 and 6.0) Google's workbook uses, scaled down to
windows that fit this project's in-memory retention instead of the
original hour/day windows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class BurnRateRule:
    """One long/short window pair and the threshold both must clear
    before this rule fires. `name` is just a label for which kind of
    leak this rule is tuned for (a sudden spike vs. a slow burn)."""

    name: str
    long_window_seconds: float
    short_window_seconds: float
    threshold: float


# Google's SRE workbook pairs: a 1h/5m fast-burn rule at 14.4x (a
# sudden spike exhausts the 30-day budget in under a day) and a
# 6h/30m slow-burn rule at 6x (a slow leak exhausts it in a few
# days). Same ratio (12:1) and same thresholds here, scaled down to
# windows that fit comfortably inside this project's default
# 900-second telemetry retention instead of hours and days.
DEFAULT_MULTI_WINDOW_RULES: List[BurnRateRule] = [
    BurnRateRule(name="fast_burn", long_window_seconds=60.0, short_window_seconds=5.0,
                 threshold=14.4),
    BurnRateRule(name="slow_burn", long_window_seconds=300.0, short_window_seconds=25.0,
                 threshold=6.0),
]


@dataclass(frozen=True)
class MultiWindowSLO:
    """An SLO plus the set of burn-rate rules checked against it.
    `slo.window_seconds` isn't used for the multi-window check itself
    (each rule brings its own windows) -- it's kept so the single
    SLO target/error_budget is defined in one place and single-window
    callers (compute_burn_rate, the existing /ops/burn-rate endpoint)
    and multi-window callers agree on what "99.5% availability" means."""

    slo: SLO
    rules: List[BurnRateRule] = field(default_factory=lambda: list(DEFAULT_MULTI_WINDOW_RULES))


@dataclass(frozen=True)
class RuleResult:
    rule_name: str
    long_window_burn_rate: BurnRateResult
    short_window_burn_rate: BurnRateResult
    triggered: bool  # both windows independently clear this rule's threshold


@dataclass(frozen=True)
class MultiWindowResult:
    tenant_id: str
    slo_name: str
    rule_results: List[RuleResult]
    should_page: bool  # True if ANY rule triggered


def evaluate_multi_window(
    telemetry: TelemetryStore,
    tenant_id: str,
    multi_window_slo: MultiWindowSLO,
    now: float | None = None,
) -> MultiWindowResult:
    """The actual multi-window, multi-burn-rate check: for each rule,
    compute burn rate over both its long and short window, and only
    count the rule as triggered if both independently clear its
    threshold. Requiring both is what buys the fast-recovery property
    a single long window doesn't have -- once the short window's
    burn rate drops back down, the rule stops firing immediately,
    even though the long window is still "contaminated" by the
    incident for a while longer."""
    rule_results = []
    for rule in multi_window_slo.rules:
        # Both windows share the base SLO's target/error_budget; only
        # window_seconds and the rule's own threshold differ between
        # the long and short check, so build throwaway SLOs per window.
        short_slo = SLO(
            name=multi_window_slo.slo.name,
            target=multi_window_slo.slo.target,
            window_seconds=rule.short_window_seconds,
        )
        long_slo = SLO(
            name=multi_window_slo.slo.name,
            target=multi_window_slo.slo.target,
            window_seconds=rule.long_window_seconds,
        )
        long_result = compute_burn_rate(
            telemetry, tenant_id, long_slo, now=now, page_threshold=rule.threshold,
        )
        short_result = compute_burn_rate(
            telemetry, tenant_id, short_slo, now=now, page_threshold=rule.threshold,
        )
        triggered = long_result.should_page and short_result.should_page
        rule_results.append(RuleResult(
            rule_name=rule.name,
            long_window_burn_rate=long_result,
            short_window_burn_rate=short_result,
            triggered=triggered,
        ))
    return MultiWindowResult(
        tenant_id=tenant_id,
        slo_name=multi_window_slo.slo.name,
        rule_results=rule_results,
        should_page=any(r.triggered for r in rule_results),
    )


def check_all_tenants_multi_window(
    telemetry: TelemetryStore, multi_window_slo: MultiWindowSLO, now: float | None = None
) -> List[MultiWindowResult]:
    """Multi-window equivalent of check_all_tenants: one on-call
    sweep call covering every tenant telemetry has seen."""
    return [
        evaluate_multi_window(telemetry, tenant_id, multi_window_slo, now)
        for tenant_id in telemetry.known_tenants()
    ]
