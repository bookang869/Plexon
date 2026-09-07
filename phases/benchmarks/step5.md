# Step 5: runner-and-docs

## Files to read

First read the following files to understand the project's architecture and design intent:

- `README.md` — full file, especially `## Getting Started` (existing command list style), `## Build Plan` (the phase-status table you're appending a row to), and `## Non-Goals` ("Not optimizing for raw throughput as the headline story" — the new section's framing should stay consistent with this, not oversell the benchmark numbers)
- `phases/benchmarks/step0.md` through `step4.md`'s actual outputs — the full list of files this phase created: `benchmarks/common.py`, `benchmarks/thresholds.py`, `benchmarks/conftest.py`, `benchmarks/bench_overhead.py`, `benchmarks/bench_throughput.py`, `benchmarks/bench_failover.py`, `benchmarks/bench_ratelimit_budget.py`, `benchmarks/results/` (gitignored), plus the `pytest` marker/`addopts` wiring in `pyproject.toml`
- `tests/load/locustfile.py` — the module docstring's "Run:" line — `run_all.py`'s own usage docstring should read similarly (prerequisites, exact invocation)
- `deploy/docker-compose.yml` — confirms the full-stack `up` command every prerequisite step in this phase's other steps already used

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

### 1. `benchmarks/run_all.py`

```python
"""Runs the full benchmark suite and prints a combined summary. Prerequisite:
docker compose -f deploy/docker-compose.yml up -d --build (full stack) and
uv run python3 scripts/setup_demo_teams.py, same as every individual
benchmarks/bench_*.py file already requires.

Run: uv run python3 benchmarks/run_all.py
"""

def main() -> int:
    """Invokes pytest.main(["-m", "benchmark", "-v", str(Path(__file__).parent)])
    to run every bench_*.py file, then reads back every benchmarks/results/
    *.json file written during that run (common.write_result already wrote
    them, including for any test whose threshold assertion failed) and
    prints one combined table (name, key metric(s), pass/fail) via
    common.print_summary or an equivalent aggregate printer. Returns
    pytest.main's own exit code so `python3 benchmarks/run_all.py` fails the
    process (non-zero exit) if any benchmark's threshold assertion failed,
    same as running pytest directly would."""

if __name__ == "__main__":
    raise SystemExit(main())
```

Don't reimplement pytest's own collection/execution/reporting — this is a thin wrapper that runs the suite via `pytest.main` and adds one aggregate view on top of the per-file results `write_result` already persists.

### 2. `README.md`

Add a new `## Performance Benchmarks` section (place it after `## Build Plan`, before `## Non-Goals`), covering:
- Prerequisites: full docker-compose stack up, `scripts/setup_demo_teams.py` run — same as the Locust prerequisite already documented for that tool, if it's mentioned elsewhere in the README (check `## Core Features` / `## Getting Started` for how the existing Locust invocation is or isn't already documented there, and match that precedent).
- What's measured: the five areas from this phase (throughput, gateway overhead P50/P95/P99, failover reliability, failover/recovery latency, rate-limit/budget accuracy under concurrency).
- How to run: `uv run pytest benchmarks/ -m benchmark -v` or `uv run python3 benchmarks/run_all.py`.
- Where results land: `benchmarks/results/*.json` / `.csv` (gitignored, regenerated each run).
- One explicit sentence distinguishing this suite from `tests/load/locustfile.py`: this is a fast, single-process, locally-run regression signal; Locust remains the tool covering TRD §10's large-scale "5,000+ concurrent requests" requirement.

Also add one row to the `## Build Plan` table:

```
| 5 | `benchmarks` | Performance benchmark suite: throughput, gateway overhead, failover reliability/recovery, rate-limit/budget accuracy under concurrency | 🔨 this PR |
```

Update the preceding sentence ("Implementation is split into 5 Harness phases...") to say "5 Harness phases covering the original build plan, plus a `benchmarks` phase added afterward" (or similar) rather than silently changing the number to 6 with no explanation — this phase was added after the original plan, not part of it from the start.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build
uv run python3 scripts/setup_demo_teams.py
uv run python3 benchmarks/run_all.py
docker compose -f deploy/docker-compose.yml down
uv run pytest -v   # full existing suite (non-benchmark) still passes, unaffected
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does `run_all.py`'s exit code reflect whether any benchmark's assertion failed (non-zero on failure), rather than always returning 0?
   - Does the new README section explicitly distinguish this suite from the Locust load test rather than implying redundant/overlapping coverage?
   - Does the README continue to make clear (per `## Non-Goals`) that this project isn't optimizing for raw throughput as its headline story — the new section should read as an assessment tool, not a marketing claim?
3. Based on the result, update `phases/benchmarks/index.json` step 5:
   - Success → `"status": "completed"`, `"summary": "one-line summary — run_all.py's behavior, the README section added, the Build Plan table row -- marking the benchmarks phase complete"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't edit `docs/PRD.md`, `docs/TRD.md`, or `docs/ADR.md` (specifically TRD §12's "5 Harness phases" table or ADR-024's "5 Automated Phases" decision). Reason: those documents record what was decided *at plan time* — retroactively rewriting them to describe a 6th phase that didn't exist when those decisions were made would misrepresent the project's actual planning history. `README.md` is the right place to reflect current reality; the docs/ planning documents are not.
- Don't reimplement percentile math, report writing, or pytest's own test collection inside `run_all.py`. Reason: `benchmarks/common.py` (step 0) already provides all of this — `run_all.py`'s only new job is running the suite as one command and printing one combined view across files.
- Don't make `run_all.py` always exit 0. Reason: a benchmark suite that can never signal failure (e.g. from a CI job or a script) is silently useless as a regression check.
- Do not break existing tests.
