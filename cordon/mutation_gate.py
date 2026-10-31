"""
mutation_gate.py -- the logic behind the CI mutation-score gate.
Lives in the cordon package (rather than directly in scripts/) so it
is itself covered by this project's own tests and mutation testing,
not exempted from the discipline it enforces on everything else.

Reads mutmut's own CI/CD stats JSON (`mutmut export-cicd-stats`) and
decides pass/fail against a floor, not a ratchet: the known
equivalent mutants documented in README.md's "Test automation"
section put a real ceiling below 100% that no test can close, so a
ratchet-only check would eventually demand the impossible. A floor
instead catches a real regression in test quality against an
already-reviewed baseline, which is the thing CI actually needs to
catch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

DEFAULT_MIN_SCORE = 0.70


class GateResult(NamedTuple):
    killed: int
    total: int
    score: float
    min_score: float
    passed: bool


def evaluate(stats_path: Path, min_score: float = DEFAULT_MIN_SCORE) -> GateResult:
    stats = json.loads(stats_path.read_text())
    killed = stats["killed"]
    total = stats["total"]
    if total == 0:
        return GateResult(killed=0, total=0, score=0.0, min_score=min_score, passed=False)
    score = killed / total
    return GateResult(
        killed=killed, total=total, score=score, min_score=min_score, passed=score >= min_score
    )
