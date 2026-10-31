"""
telemetry.py -- per-tenant request telemetry: what actually
happened, kept long enough for slo.py to compute a burn rate from
it. No model, no judgment -- just a rolling, timestamped record of
outcomes, per tenant, so a noisy tenant's requests never get
averaged in with anyone else's.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List

SUCCESS = "success"
ERROR = "error"
THROTTLED = "throttled"  # budget.BudgetExceeded -- not a server error


@dataclass(frozen=True)
class RequestEvent:
    tenant_id: str
    timestamp: float
    latency_ms: float
    outcome: str  # SUCCESS | ERROR | THROTTLED


class TelemetryStore:
    """Keeps every request event, per tenant, for a bounded recent
    window (old events are dropped lazily on read, never eagerly on
    write, so a write is always O(1)). This is intentionally an
    in-memory list, not a real metrics backend -- the point of this
    project is the isolation and burn-rate logic, not standing up
    Prometheus."""

    def __init__(self, retention_seconds: float = 900.0) -> None:
        self._retention_seconds = retention_seconds
        self._events: Dict[str, List[RequestEvent]] = {}

    def record(
        self, tenant_id: str, latency_ms: float, outcome: str, now: float | None = None
    ) -> None:
        now = now if now is not None else time.monotonic()
        self._events.setdefault(tenant_id, []).append(
            RequestEvent(tenant_id=tenant_id, timestamp=now, latency_ms=latency_ms, outcome=outcome)
        )

    def events_in_window(
        self, tenant_id: str, window_seconds: float, now: float | None = None
    ) -> List[RequestEvent]:
        now = now if now is not None else time.monotonic()
        cutoff = now - window_seconds
        events = self._events.get(tenant_id, [])
        # Prune anything older than the full retention window while we're
        # here -- keeps memory bounded without a background sweep thread.
        retained_cutoff = now - self._retention_seconds
        events[:] = [e for e in events if e.timestamp >= retained_cutoff]
        return [e for e in events if e.timestamp >= cutoff]

    def known_tenants(self) -> List[str]:
        return list(self._events.keys())
