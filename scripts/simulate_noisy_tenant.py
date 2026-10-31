"""
simulate_noisy_tenant.py -- a deliberately simulated incident, used
to produce the real numbers in incident/postmortem.md.

Honesty note, stated here and repeated in the postmortem: this is a
synthetic load test against the in-process service (no real network,
no real customers) -- it exists to demonstrate that the isolation
mechanism and the telemetry/SLO layer actually do what they claim,
not to stand in for a real production incident. The numbers it
prints are real outputs of real code, run deterministically; they
are not fabricated or hand-picked.

Scenario: "noisy" sends far more requests than its budget allows
(simulating a runaway client or a bug in a tenant's integration);
"quiet" sends a modest, steady stream throughout. The question this
answers: does "quiet" notice anything at all?
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cordon.budget import BudgetExceeded  # noqa: E402
from cordon.service import CaseService  # noqa: E402
from cordon.slo import SLO, compute_burn_rate, throttled_fraction  # noqa: E402

NOISY_REQUESTS = 80
QUIET_REQUESTS = 15
SLO_DEF = SLO(name="availability", target=0.995, window_seconds=300.0)


def run() -> dict:
    # Deliberately no fixed "now" anywhere below: every call defaults to
    # the real clock (time.monotonic()), the same clock CaseService used
    # while recording the requests -- fixing "now" here without fixing
    # it inside the service too would read back a window that doesn't
    # overlap with when the events actually happened.
    service = CaseService()

    noisy_outcomes = {"success": 0, "throttled": 0}
    for i in range(NOISY_REQUESTS):
        try:
            service.create_case("noisy-tenant", f"runaway request {i}")
            noisy_outcomes["success"] += 1
        except BudgetExceeded:
            noisy_outcomes["throttled"] += 1

    quiet_outcomes = {"success": 0, "throttled": 0}
    for i in range(QUIET_REQUESTS):
        try:
            service.create_case("quiet-tenant", f"ordinary request {i}")
            quiet_outcomes["success"] += 1
        except BudgetExceeded:
            quiet_outcomes["throttled"] += 1

    noisy_burn = compute_burn_rate(service.telemetry, "noisy-tenant", SLO_DEF)
    quiet_burn = compute_burn_rate(service.telemetry, "quiet-tenant", SLO_DEF)
    noisy_throttled_frac = throttled_fraction(
        service.telemetry, "noisy-tenant", SLO_DEF.window_seconds
    )
    quiet_throttled_frac = throttled_fraction(
        service.telemetry, "quiet-tenant", SLO_DEF.window_seconds
    )

    return {
        "scenario": "noisy-tenant sends 80 requests against a budget sized for far fewer; "
        "quiet-tenant sends 15 ordinary requests throughout",
        "noisy_tenant": {
            "requests_sent": NOISY_REQUESTS,
            "outcomes": noisy_outcomes,
            "throttled_fraction": round(noisy_throttled_frac, 3),
            "slo_burn_rate": round(noisy_burn.burn_rate, 3),
            "slo_should_page": noisy_burn.should_page,
            "headroom": service.budgets.headroom("noisy-tenant"),
        },
        "quiet_tenant": {
            "requests_sent": QUIET_REQUESTS,
            "outcomes": quiet_outcomes,
            "throttled_fraction": round(quiet_throttled_frac, 3),
            "slo_burn_rate": round(quiet_burn.burn_rate, 3),
            "slo_should_page": quiet_burn.should_page,
            "headroom": service.budgets.headroom("quiet-tenant"),
        },
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
    out_path = Path(__file__).resolve().parent.parent / "incident" / "simulation_output.json"
    out_path.write_text(json.dumps(result, indent=2) + "\n")
    print(f"\nWritten to {out_path}", file=sys.stderr)
