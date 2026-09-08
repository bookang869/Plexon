# Step 0: throughput-sweep

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ARCHITECTURE.md`
- `/docs/ADR.md`
- `/benchmarks/bench_throughput.py` — the existing single-concurrency sustained-throughput benchmark this step extends
- `/benchmarks/common.py` — `run_concurrent`, `percentiles`, `BenchmarkResult`, `write_result` (do not modify this file; its existing flat `dict[str, float]` metrics contract must keep working for every other `bench_*.py` file)
- `/benchmarks/thresholds.py` — `THROUGHPUT_MIN_RPS`, `THROUGHPUT_P95_MS`
- `/benchmarks/run_all.py` — confirms result discovery is a generic `results/*.json` glob, so new result file names are safe to add
- `/pyproject.toml`'s `[tool.pytest.ini_options]` — confirms no random test-order plugin is configured, so this module's default file-order execution (parametrize list order, then a final test defined after) is safe to rely on

Read carefully through the code produced in the prior `benchmarks` phase (`phases/benchmarks/index.json` has the per-step summaries) and understand the design intent before working.

## Task

The current `test_sustained_throughput` in `benchmarks/bench_throughput.py` runs at a single fixed concurrency (default 10, via `PLEXON_BENCHMARK_CONCURRENCY`). Replace it with a sweep across `[5, 20, 50, 100, 200]` concurrent workers so the benchmark reports how throughput scales, and what the maximum concurrency is that the gateway can sustain without errors.

1. Factor the existing per-run logic (build payload, run `run_concurrent` in duration mode, compute `achieved_rps`/`error_rate`/latency percentiles) into a helper, e.g.:

   ```python
   async def _run_at_concurrency(client: httpx.AsyncClient, headers: dict, concurrency: int, duration_seconds: float) -> dict:
       ...  # returns {"concurrency", "achieved_rps", "total_requests", "error_count", "error_rate", "p50_ms", "p95_ms", "p99_ms"}
   ```

2. Add a module-level sweep list, env-overridable the same way the existing `_CONCURRENCY`/`_DURATION_SECONDS` constants are:

   ```python
   _SWEEP_CONCURRENCIES = [int(c) for c in os.environ.get("PLEXON_BENCHMARK_SWEEP_CONCURRENCIES", "5,20,50,100,200").split(",")]
   ```

3. Add `@pytest.mark.parametrize("concurrency", _SWEEP_CONCURRENCIES)` on a renamed `test_throughput_sweep(concurrency, benchmark_team)`. Each invocation calls `_run_at_concurrency` and writes its own result via `write_result(BenchmarkResult(name=f"throughput_c{concurrency}", ...))`. **Do not hard-assert `error_rate == 0` or the RPS threshold inside this per-level test** — some of the higher concurrency levels are expected to reveal degradation; that's the point of the sweep, not a bug to fail the test suite over. Append each level's metrics dict to a module-level list (e.g. `_sweep_results: list[dict] = []`) as it completes, so the summary step below can read them back without touching disk.

4. Add one more test defined *after* the parametrized one in the same file, e.g. `test_throughput_sweep_summary()` (no fixture needed beyond what's already been populated by step 3's module-level list). It must:
   - Find the highest concurrency level in `_sweep_results` where `error_rate == 0.0`, and record its `achieved_rps` as `max_sustainable_rps` / its concurrency as `max_sustainable_concurrency` (both `None` if no level is clean).
   - Write one `BenchmarkResult(name="throughput_sweep_summary", metrics={"max_sustainable_concurrency": ..., "max_sustainable_rps": ..., "levels": _sweep_results, ...}, thresholds={"min_rps": thresholds.THROUGHPUT_MIN_RPS}, passed=...)`. `metrics` may hold a nested list (`common.write_result`'s CSV writer will just stringify it via `str(value)` for that one row — acceptable, the JSON file is the source of truth for the per-level table).
   - Assert `max_sustainable_rps is not None and max_sustainable_rps >= thresholds.THROUGHPUT_MIN_RPS` — i.e., at least one concurrency level must clear the existing floor cleanly.

5. Update the module docstring to describe the sweep (keep the existing Locust-comparison paragraph; just add a sentence about what levels are swept and why).

6. Update `README.md`'s `## Performance Benchmarks` section, "What's measured" bullet for throughput, to mention it's now a concurrency sweep (5→20→50→100→200) reporting maximum sustainable RPS, not a single fixed-concurrency number.

## Acceptance Criteria

```bash
docker compose -f deploy/docker-compose.yml up -d --build
python3 scripts/setup_demo_teams.py
uv run pytest benchmarks/bench_throughput.py -m benchmark -v
ruff check benchmarks/ README.md
```

All 6 tests (5 sweep levels + 1 summary) must run and the summary must pass. Note this will take longer than the old single-concurrency test (5 levels × `PLEXON_BENCHMARK_DURATION_SECONDS`, default 5s each ⇒ ~25s total) — that's expected.

## Verification Procedure

1. Run the AC commands above.
2. Confirm `benchmarks/results/throughput_c5.json` … `throughput_c200.json` and `throughput_sweep_summary.json` all exist and contain plausible numbers (RPS should generally rise then plateau or drop as concurrency increases; check this isn't nonsensical, e.g. all zeros).
3. Confirm the rest of the suite is untouched: `uv run pytest -m benchmark -v` still collects and can run every other `bench_*.py` file without import errors.
4. Confirm the CRITICAL rules in CLAUDE.md aren't violated (no state added to gateway process memory, no new Postgres/YAML config split violated — this step touches only the benchmark harness, not gateway code).
5. Based on the result, update this step's entry in `phases/benchmark-scale/index.json`:
   - Success → `"status": "completed"`, `"summary": "one-line summary of the output"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't change `benchmarks/common.py`'s `BenchmarkResult`/`write_result` contract. Reason: every other `bench_*.py` file depends on its current flat-dict CSV behavior; this step's nested `"levels"` value is tolerated by the existing code as-is, no signature change needed.
- Don't hard-fail the per-level parametrized tests on RPS/error-rate thresholds. Reason: degradation at high concurrency is the expected, useful signal this sweep exists to surface — only the summary test should gate the suite.
- Don't remove or repurpose the existing `THROUGHPUT_MIN_RPS`/`THROUGHPUT_P95_MS` threshold constants. Reason: other docs/tests may reference them; the summary step reuses `THROUGHPUT_MIN_RPS` as-is.
- Don't break existing tests.
