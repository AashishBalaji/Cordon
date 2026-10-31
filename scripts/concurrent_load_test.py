"""
concurrent_load_test.py -- closes the gap `incident/postmortem.md`
named explicitly: the original noisy-tenant simulation
(`simulate_noisy_tenant.py`) ran single-threaded, sequentially, so it
never exercised whether budget enforcement holds under real
concurrent access, only whether the arithmetic is correct when
called one request at a time.

This re-runs the same scenario (a flood from "noisy-tenant" against
a modest, steady "quiet-tenant") through `cordon.service.CaseService`
using real `threading.Thread`s submitted at once via a thread pool --
not asyncio, not a loop with `time.sleep`, actual OS-level concurrent
calls into the same `BudgetRegistry`/`TelemetryStore` a real
multi-threaded server process would share.

Honesty note, same as the original script: this is still a synthetic,
in-process load test -- real threads, but no real network, no real
customers. What it adds over the original is confidence that the
isolation guarantee holds under genuine concurrent access, which is
the thing `tests/test_concurrency.py` tests directly and in isolation;
this script is the same finding presented as a load-test narrative,
and is also a real documented finding from building this: the
pre-fix `TenantBudget.charge()` had a reproducible check-then-take
race under real threads (see that test file's module docstring and
cordon/budget.py's concurrency note) -- this script's "requests
accepted never exceeds the rate budget's capacity" assertion is
exactly the property that race used to violate.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cordon.budget import BudgetExceeded  # noqa: E402
from cordon.service import CaseService  # noqa: E402
from cordon.slo import SLO, compute_burn_rate  # noqa: E402

NOISY_REQUESTS = 80
QUIET_REQUESTS = 15
POOL_WORKERS = 32  # real OS threads, submitted concurrently, not sequentially
SLO_DEF = SLO(name="availability", target=0.995, window_seconds=300.0)


def _fire(service: CaseService, tenant_id: str, label: str, counts: dict, lock: Lock) -> None:
    try:
        service.create_case(tenant_id, label)
        outcome = "success"
    except BudgetExceeded:
        outcome = "throttled"
    with lock:
        counts[outcome] += 1


def run() -> dict:
    service = CaseService()
    noisy_counts = {"success": 0, "throttled": 0}
    quiet_counts = {"success": 0, "throttled": 0}
    counts_lock = Lock()

    # Both tenants' requests are interleaved in the SAME submission
    # batch, run by the SAME thread pool -- this is what the original
    # sequential script couldn't show: whether noisy-tenant's flood,
    # hitting the service at the same real-world instant as
    # quiet-tenant's traffic, ever lets the two interfere.
    jobs = [("noisy-tenant", f"runaway request {i}", noisy_counts) for i in range(NOISY_REQUESTS)]
    jobs += [("quiet-tenant", f"ordinary request {i}", quiet_counts) for i in range(QUIET_REQUESTS)]

    with ThreadPoolExecutor(max_workers=POOL_WORKERS) as pool:
        futures = [
            pool.submit(_fire, service, tenant_id, label, counts, counts_lock)
            for tenant_id, label, counts in jobs
        ]
        for f in as_completed(futures):
            f.result()  # re-raise anything unexpected rather than swallow it

    noisy_burn = compute_burn_rate(service.telemetry, "noisy-tenant", SLO_DEF)
    quiet_burn = compute_burn_rate(service.telemetry, "quiet-tenant", SLO_DEF)

    rate_capacity = int(service.budgets.get("noisy-tenant").rate_bucket.capacity)
    correctness_check = {
        "rate_budget_capacity": rate_capacity,
        "noisy_successes": noisy_counts["success"],
        "accepted_never_exceeds_capacity": noisy_counts["success"] <= rate_capacity,
    }

    return {
        "scenario": "Same noisy/quiet-tenant scenario as simulate_noisy_tenant.py, but all "
        f"{NOISY_REQUESTS + QUIET_REQUESTS} requests from both tenants submitted at once to a "
        f"{POOL_WORKERS}-worker real thread pool, not run sequentially in a loop.",
        "noisy_tenant": {
            "requests_sent": NOISY_REQUESTS,
            "outcomes": noisy_counts,
            "slo_burn_rate": round(noisy_burn.burn_rate, 3),
            "slo_should_page": noisy_burn.should_page,
            "headroom": service.budgets.headroom("noisy-tenant"),
        },
        "quiet_tenant": {
            "requests_sent": QUIET_REQUESTS,
            "outcomes": quiet_counts,
            "slo_burn_rate": round(quiet_burn.burn_rate, 3),
            "slo_should_page": quiet_burn.should_page,
            "headroom": service.budgets.headroom("quiet-tenant"),
        },
        "concurrency_correctness_check": correctness_check,
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
    if not result["concurrency_correctness_check"]["accepted_never_exceeds_capacity"]:
        print(
            "\nFAIL: noisy-tenant's accepted requests exceeded its rate budget capacity "
            "under real concurrency -- see cordon/budget.py's locking.",
            file=sys.stderr,
        )
        sys.exit(1)
    out_path = Path(__file__).resolve().parent.parent / "incident" / "concurrent_load_output.json"
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"\nWritten to {out_path}", file=sys.stderr)
