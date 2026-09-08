"""Unit tests for benchmarks/run_all.py's result-aggregation helper. Not
marked `benchmark` -- this only reads pre-written JSON fixtures, it doesn't
talk to a live gateway.
"""

from __future__ import annotations

import json

from benchmarks.run_all import _print_combined_summary


def test_print_combined_summary_no_results(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("benchmarks.run_all.RESULTS_DIR", tmp_path)

    _print_combined_summary()

    assert "No benchmark results found" in capsys.readouterr().out


def test_print_combined_summary_prints_pass_and_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("benchmarks.run_all.RESULTS_DIR", tmp_path)
    (tmp_path / "one.json").write_text(
        json.dumps({"name": "one", "metrics": {"rps": 77.0}, "passed": True})
    )
    (tmp_path / "two.json").write_text(
        json.dumps({"name": "two", "metrics": {"p95_ms": 30.0}, "passed": False})
    )

    _print_combined_summary()

    out = capsys.readouterr().out
    assert "[PASS] one: rps=77.0" in out
    assert "[FAIL] two: p95_ms=30.0" in out
