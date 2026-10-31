"""
metrics.py -- Prometheus-format telemetry, alongside the custom
JSON /ops/* endpoints already in api.py.

Why both: the /ops/* endpoints are for a human clicking around
during an incident (readable, one tenant at a time); /metrics is for
a real monitoring stack (Prometheus, Grafana, an alerting pipeline)
to scrape on an interval and graph over time. Shipping both is the
honest answer to "how would telemetry actually get used operationally" --
a hand-rolled JSON endpoint is not what a production on-call setup
scrapes.

Because the set of tenants is only known at request time (there's no
fixed metric registered per tenant up front), this uses a custom
Collector -- the officially supported `prometheus_client` pattern for
metrics whose label values aren't known until scrape time -- rather
than pre-declaring a Gauge per tenant, which would require knowing
every tenant in advance.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .budget import BudgetRegistry
from .slo import SLO, MultiWindowSLO, compute_burn_rate, evaluate_multi_window
from .telemetry import ERROR, SUCCESS, THROTTLED, TelemetryStore


class CordonCollector:
    """Pulls current state from a TelemetryStore and BudgetRegistry
    at scrape time and yields it as Prometheus metric families, one
    tenant per label value. No state of its own -- it reads, never
    writes, same as every other telemetry consumer in this project."""

    def __init__(self, telemetry: TelemetryStore, budgets: BudgetRegistry, slo: SLO):
        self._telemetry = telemetry
        self._budgets = budgets
        self._slo = slo
        self._multi_window_slo = MultiWindowSLO(slo=slo)

    def collect(self):
        tenants = self._telemetry.known_tenants()

        requests_total = CounterMetricFamily(
            "cordon_requests_total",
            "Total requests recorded per tenant per outcome, since process start.",
            labels=["tenant_id", "outcome"],
        )
        for tenant_id in tenants:
            events = self._telemetry.all_events(tenant_id)
            counts = {SUCCESS: 0, ERROR: 0, THROTTLED: 0}
            for event in events:
                counts[event.outcome] = counts.get(event.outcome, 0) + 1
            for outcome, count in counts.items():
                requests_total.add_metric([tenant_id, outcome], count)
        yield requests_total

        burn_rate = GaugeMetricFamily(
            "cordon_slo_burn_rate",
            "Current SLO error-budget burn rate per tenant (1.0 = burning exactly as allowed).",
            labels=["tenant_id"],
        )
        should_page = GaugeMetricFamily(
            "cordon_slo_should_page",
            "Whether this tenant's current burn rate crosses the page threshold (1) or not (0).",
            labels=["tenant_id"],
        )
        for tenant_id in tenants:
            result = compute_burn_rate(self._telemetry, tenant_id, self._slo)
            burn_rate.add_metric([tenant_id], result.burn_rate)
            should_page.add_metric([tenant_id], 1.0 if result.should_page else 0.0)
        yield burn_rate
        yield should_page

        should_page_multi_window = GaugeMetricFamily(
            "cordon_slo_should_page_multi_window",
            "Whether this tenant's fast-burn or slow-burn multi-window rule has "
            "triggered (1) or not (0) -- the real paging decision, see slo.py.",
            labels=["tenant_id"],
        )
        for tenant_id in tenants:
            mw_result = evaluate_multi_window(self._telemetry, tenant_id, self._multi_window_slo)
            should_page_multi_window.add_metric(
                [tenant_id], 1.0 if mw_result.should_page else 0.0
            )
        yield should_page_multi_window

        headroom = GaugeMetricFamily(
            "cordon_budget_headroom_ratio",
            "Fraction of each per-tenant budget still available (0.0-1.0).",
            labels=["tenant_id", "budget"],
        )
        for tenant_id in tenants:
            for budget_name, fraction in self._budgets.headroom(tenant_id).items():
                headroom.add_metric([tenant_id, budget_name], fraction)
        yield headroom


def render_metrics(telemetry: TelemetryStore, budgets: BudgetRegistry, slo: SLO) -> bytes:
    """Build a fresh registry per call rather than relying on
    prometheus_client's process-global default registry -- keeps this
    testable (no cross-test state) and correct if multiple CaseService
    instances ever exist in the same process."""
    registry = CollectorRegistry()
    registry.register(CordonCollector(telemetry, budgets, slo))
    return generate_latest(registry)
