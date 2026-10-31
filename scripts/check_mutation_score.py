"""
check_mutation_score.py -- thin CLI wrapper around
cordon.mutation_gate.evaluate(). The gate logic itself lives in the
package (see that module's docstring for why) so it's covered by
this project's own test suite and mutation testing; this script is
just the command CI actually runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cordon.mutation_gate import DEFAULT_MIN_SCORE, evaluate  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_mutation_score.py <stats.json> [min_score]", file=sys.stderr)
        return 2
    stats_path = Path(argv[0])
    min_score = float(argv[1]) if len(argv) > 1 else DEFAULT_MIN_SCORE

    result = evaluate(stats_path, min_score)
    if result.total == 0:
        print("no mutants were generated -- treat this as a failure, not a pass", file=sys.stderr)
        return 1

    print(f"mutation score: {result.killed}/{result.total} killed = {result.score:.1%} "
          f"(floor: {result.min_score:.0%})")
    if not result.passed:
        print("FAIL: mutation score dropped below the floor", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
