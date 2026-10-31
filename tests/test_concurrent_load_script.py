"""Smoke test for scripts/concurrent_load_test.py as a real script --
imports and runs its run() function directly (fast, no subprocess
needed since it has no CLI arguments) and asserts the same
correctness property the script checks for itself: a real concurrent
flood never gets more requests accepted than the rate budget's
capacity allows."""

import importlib
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"

# Same sandboxing caveat as test_check_mutation_score_cli.py: mutmut's
# sandbox only contains cordon/ and tests/, not scripts/.
pytestmark = pytest.mark.skipif(
    not (SCRIPT_DIR / "concurrent_load_test.py").exists(),
    reason="scripts/ not present in this sandbox",
)


def test_concurrent_load_run_never_overdraws_the_budget():
    sys.path.insert(0, str(SCRIPT_DIR))
    import concurrent_load_test as script  # noqa: E402

    importlib.reload(script)
    result = script.run()

    check = result["concurrency_correctness_check"]
    assert check["accepted_never_exceeds_capacity"] is True
    assert check["noisy_successes"] == check["rate_budget_capacity"]
    assert result["quiet_tenant"]["outcomes"]["throttled"] == 0
