"""
telemetry.py -- per-tenant request telemetry: what actually
happened, kept long enough for slo.py to compute a burn rate from
it. No model, no judgment -- just a rolling, timestamped record of
outcomes, per tenant, so a noisy tenant's requests never get
averaged in with anyone else's.

Concurrency: a single `threading.Lock` guards every method, not a
per-tenant one like budget.py uses. The reason for the difference is
`events_in_window`'s prune step (`events[:] = [...]`), which rewrites
the *whole* per-tenant list in place -- a concurrent `record()` call
appending to that same list object while a prune is mid-rewrite could
silently lose the appended event. A per-tenant lock would still need
to be the same lock for both methods on the same tenant, and the
store-wide lock is held only for a plain list filter/append, cheap
enough that one global lock doesn't become the bottleneck tenant
isolation is supposed to prevent (see `tests/test_concurrency.py`).
"""

from __future__ import annotations

import threading
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
        self._lock = threading.Lock()

    def record(
        self, tenant_id: str, latency_ms: float, outcome: str, now: float | None = None
    ) -> None:
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._events.setdefault(tenant_id, []).append(
                RequestEvent(
                    tenant_id=tenant_id, timestamp=now, latency_ms=latency_ms, outcome=outcome
                )
            )

    def events_in_window(
        self, tenant_id: str, window_seconds: float, now: float | None = None
    ) -> List[RequestEvent]:
        now = now if now is not None else time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events.get(tenant_id, [])
            # Prune anything older than the full retention window while
            # we're here -- keeps memory bounded without a background
            # sweep thread. Done under the same lock as record() so a
            # concurrent append can never be lost mid-rewrite.
            retained_cutoff = now - self._retention_seconds
            events[:] = [e for e in events if e.timestamp >= retained_cutoff]
            return [e for e in events if e.timestamp >= cutoff]

    def known_tenants(self) -> List[str]:
        with self._lock:
            return list(self._events.keys())

    def all_events(self, tenant_id: str) -> List[RequestEvent]:
        """Every retained event for this tenant, with no window
        cutoff -- for callers (metrics.py) that want "since process
        start" counts rather than a recent window, and that must not
        depend on how the caller's own clock relates to retention,
        the way events_in_window()'s pruning does."""
        with self._lock:
            return list(self._events.get(tenant_id, []))
