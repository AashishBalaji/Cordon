"""Smoke test for scripts/check_mutation_score.py as an actual CLI --
run as a subprocess so it's exercised the same way CI invokes it,
rather than importing its internals (which live in cordon.mutation_gate
and are tested directly in test_mutation_gate.py)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_mutation_score.py"

# mutmut runs tests against a sandboxed copy that only contains the
# mutated package (cordon/) and tests/ -- scripts/ isn't part of that
# copy, so this subprocess-based smoke test has nothing to invoke
# there. That's fine: cordon.mutation_gate (the logic this script
# wraps) is covered directly, and by mutation testing, in
# test_mutation_gate.py -- this file only smoke-tests the CLI's own
# argument handling and exit codes, which mutmut doesn't need to see.
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="scripts/ not present in this sandbox")


def _write_stats(tmp_path, killed, total):
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"killed": killed, "total": total, "survived": total - killed}))
    return path


def test_cli_exits_zero_on_passing_score(tmp_path):
    path = _write_stats(tmp_path, killed=80, total=100)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "0.70"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "80/100" in result.stdout


def test_cli_exits_nonzero_on_failing_score(tmp_path):
    path = _write_stats(tmp_path, killed=10, total=100)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "0.70"], capture_output=True, text=True
    )
    assert result.returncode == 1


def test_cli_exits_2_with_no_arguments():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2
