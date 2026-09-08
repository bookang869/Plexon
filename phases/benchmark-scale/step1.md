# Step 1: overhead-sweep

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ARCHITECTURE.md`
- `/docs/ADR.md`
- `/benchmarks/bench_overhead.py` — the existing single-concurrency (5) gateway-overhead benchmark this step extends
- `/benchmarks/bench_throughput.py` (as modified in step 0 of this phase) — mirror its sweep pattern (module-level `_SWEEP_CONCURRENCIES` env var, parametrize + module-level accumulator list + trailing summary test) for consistency across the two benchmark files
- `/benchmarks/common.py` — `histogram_percentiles_from_metrics_text`, `run_concurrent`, `BenchmarkResult`, `write_result` (do not modify)
- `/benchmarks/thresholds.py` — `OVERHEAD_P95_MS` and its calibration comment (doubled from TRD's 10ms target specifically to leave headroom at low concurrency on a dev machine — this threshold was never calibrated for higher concurrency levels, see Task below)

## Task

The current `test_gateway_overhead_percentiles` measures `gateway_overhead_seconds` P50/P95/P99 at a single fixed concurrency of 5. Extend it to sweep the same concurrency levels as step 0's throughput sweep, so overhead-under-load is visible, while preserving the existing calibrated regression gate at concurrency=5 (raising it to every swept level would produce false failures at higher concurrency that `OVERHEAD_P95_MS` was never calibrated for).

1. Reuse the same env var as step 0 for the sweep list: `_SWEEP_CONCURRENCIES = [int(c) for c in os.environ.get("PLEXON_BENCHMARK_SWEEP_CONCURRENCIES", "5,20,50,100,200").split(",")]` (same var name and default, so a single env override sweeps both benchmarks consistently).

2. Factor the existing warmup + before/after `/metrics` snapshot + delta-histogram + percentile logic into a helper, e.g.:

   ```python
   async def _measure_overhead_at_concurrency(client: httpx.AsyncClient, headers: dict, concurrency: int) -> dict:
       ...  # returns {"concurrency", "p50_ms", "p95_ms", "p99_ms", "total_requests"}
   ```

   Keep the existing `_WARMUP_REQUESTS` / before-after `/metrics` diffing technique (module docstring's rationale for why this is needed — cumulative process-lifetime histogram — still applies at every concurrency level). Scale `_TOTAL_REQUESTS` per level so higher concurrency still gets a meaningful sample count for percentile estimation, e.g. `total_requests = max(_TOTAL_REQUESTS, concurrency * 10)`; leave the exact scaling formula to your judgment but document it in a comment.

3. Add `@pytest.mark.parametrize("concurrency", _SWEEP_CONCURRENCIES)` on a renamed `test_overhead_sweep(concurrency, benchmark_team)`. Each invocation writes its own result via `write_result(BenchmarkResult(name=f"gateway_overhead_c{concurrency}", ...))`. Append each level's metrics dict to a module-level `_sweep_results: list[dict] = []` as it completes. **Only hard-assert the `OVERHEAD_P95_MS` threshold for the concurrency=5 level** (the level the threshold was actually calibrated against, per `thresholds.py`'s comment) — other levels should still compute and report a `passed` field against the same threshold for visibility in the JSON/CSV, but must not fail the test itself.

4. Add a trailing `test_overhead_sweep_summary()` (defined after the parametrized test) that writes one `BenchmarkResult(name="overhead_sweep_summary", metrics={"levels": _sweep_results, ...}, thresholds={"p95_ms_at_concurrency_5": thresholds.OVERHEAD_P95_MS}, passed=...)` summarizing the full table — no new assertion beyond confirming the concurrency=5 entry is present and matches what step 3 already gated.

5. Update the module docstring to mention the sweep and that only concurrency=5 is a hard gate.

6. Update `README.md`'s `## Performance Benchmarks` section, overhead bullet, to note it's measured across the concurrency sweep (with concurrency=5 as the calibrated pass/fail gate).

## Acceptance Criteria

```bash
docker compose -f deploy/docker-compose.yml up -d --build
python3 scripts/setup_demo_teams.py
uv run pytest benchmarks/bench_overhead.py -m benchmark -v
ruff check benchmarks/ README.md
```

## Verification Procedure

1. Run the AC commands above.
2. Confirm `benchmarks/results/gateway_overhead_c5.json` … `gateway_overhead_c200.json` and `overhead_sweep_summary.json` all exist with plausible numbers (P95 overhead should generally rise with concurrency; sanity-check it isn't flat/zero across levels, which would indicate the before/after diffing broke).
3. Confirm `uv run pytest -m benchmark -v` still collects every `bench_*.py` file without import errors (including step 0's changes).
4. Confirm no CRITICAL rule in CLAUDE.md is violated.
5. Based on the result, update this step's entry in `phases/benchmark-scale/index.json`:
   - Success → `"status": "completed"`, `"summary": "one-line summary of the output"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't hard-assert `OVERHEAD_P95_MS` at every swept concurrency level. Reason: the threshold was explicitly calibrated for concurrency=5 (see `thresholds.py`'s comment); asserting it at 200-concurrency would produce a false regression failure for a load level it was never meant to gate.
- Don't change `benchmarks/common.py`. Reason: same shared-contract concern as step 0.
- Don't touch `benchmarks/bench_throughput.py` in this step. Reason: step 0 already owns that file; keep this step's diff scoped to `bench_overhead.py` and `README.md`.
- Don't break existing tests.
