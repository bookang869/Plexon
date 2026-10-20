"""Runs the full benchmark suite and prints a combined summary. Prerequisite:
docker compose -f deploy/docker-compose.yml up -d --build (full stack) and
uv run python3 scripts/setup_demo_teams.py, same as every individual
benchmarks/bench_*.py file already requires.

Run: uv run python3 benchmarks/run_all.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

RESULTS_DIR = Path(__file__).parent / "results"


def _print_combined_summary() -> None:
    result_files = sorted(RESULTS_DIR.glob("*.json"))
    if not result_files:
        print("\nNo benchmark results found in benchmarks/results/.")
        return

    print("\n=== combined benchmark summary ===")
    for path in result_files:
        payload = json.loads(path.read_text())
        status = "PASS" if payload["passed"] else "FAIL"
        metrics = ", ".join(f"{k}={v}" for k, v in payload["metrics"].items())
        print(f"  [{status}] {payload['name']}: {metrics}")
    print()


def main() -> int:
    exit_code = pytest.main(["-m", "benchmark", "-v", str(Path(__file__).parent)])
    _print_combined_summary()
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
