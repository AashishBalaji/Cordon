"""Tests for cordon.mutation_gate -- the logic behind the CI
mutation-score gate, kept in the package so mutmut covers it too."""

import json

from cordon.mutation_gate import DEFAULT_MIN_SCORE, evaluate


def _write_stats(tmp_path, killed, total):
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"killed": killed, "total": total, "survived": total - killed}))
    return path


def test_passes_when_score_at_or_above_floor(tmp_path):
    path = _write_stats(tmp_path, killed=75, total=100)
    result = evaluate(path, min_score=0.70)
    assert result.passed is True
    assert result.score == 0.75


def test_fails_when_score_below_floor(tmp_path):
    path = _write_stats(tmp_path, killed=50, total=100)
    result = evaluate(path, min_score=0.70)
    assert result.passed is False


def test_score_exactly_at_floor_passes(tmp_path):
    # >=, not > -- a score exactly on the floor should not be a failure
    path = _write_stats(tmp_path, killed=70, total=100)
    result = evaluate(path, min_score=0.70)
    assert result.passed is True


def test_fails_on_zero_total_rather_than_passing_vacuously(tmp_path):
    path = _write_stats(tmp_path, killed=0, total=0)
    result = evaluate(path, min_score=0.70)
    assert result.passed is False
    assert result.total == 0


def test_default_floor_is_used_when_not_specified(tmp_path):
    path = _write_stats(tmp_path, killed=95, total=100)
    result = evaluate(path)
    assert result.min_score == DEFAULT_MIN_SCORE
    assert result.passed is True
