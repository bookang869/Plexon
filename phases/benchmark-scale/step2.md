# Step 2: failover-trials

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ARCHITECTURE.md`
- `/docs/ADR.md`
- `/benchmarks/bench_failover.py` — the existing `test_failover_reliability_and_switch_latency` (single 20-concurrent-request wave) and `anthropic_breaker` fixture this step extends
- `/benchmarks/common.py` — `run_concurrent`, `percentiles`, `BenchmarkResult`, `write_result` (do not modify)
- `/benchmarks/thresholds.py` — `FAILOVER_RELIABILITY_MIN_PCT`, `FAILOVER_SWITCH_MAX_SECONDS`
- `/tests/integration/test_concurrent_resilience.py` — the existing `anthropic_breaker`-reset technique this file's fixture already mirrors; keep the same reset semantics (Redis keys + `circuit_breaker_history` row deletion) when reusing it mid-test

## Task

The current `test_failover_reliability_and_switch_latency` runs a single 20-concurrent-request wave against the fault-injection model and reports one `reliability_pct` number (currently 100%), which is too small a sample to be confident in and gives no sense of how many requests actually back that number.

1. Extract the `anthropic_breaker` fixture's inner reset logic (the `_reset()` closure that deletes the four Redis keys and the `circuit_breaker_history` rows) into a module-level helper both the fixture and the per-trial loop can call:

   ```python
   async def _reset_anthropic_breaker(redis_client, db_pool) -> None:
       ...
   ```

   The fixture becomes a thin wrapper calling this before and after the test, same as today.

2. Add a module-level trial count, env-overridable: `_OUTAGE_TRIALS = int(os.environ.get("PLEXON_BENCHMARK_FAILOVER_TRIALS", "5"))`.

3. Modify `test_failover_reliability_and_switch_latency` to run `_OUTAGE_TRIALS` independent trials in a loop. Each trial must:
   - Call `_reset_anthropic_breaker(redis_client, db_pool)` first (the breaker must start closed each trial — otherwise a breaker left open from trial N would make trial N+1's requests fail differently, e.g. serve fallback without ever retrying the primary, skewing switch-latency numbers for reasons unrelated to reliability).
   - Run the existing 20-concurrent-request wave (`_TOTAL_REQUESTS = 20` per trial, unchanged) and collect that trial's samples.
   - Record per-trial `{"trial": i, "total_requests": ..., "success_count": ..., "reliability_pct": ...}`.

4. After all trials, compute and report both the per-trial breakdown and the aggregate:
   - `total_requests_all_trials` = sum of every trial's request count (this is the number the reliability percentage above line 1 in your message is asking for — the actual denominator behind the headline number).
   - `overall_reliability_pct` = 100 × (sum of successes across all trials) / `total_requests_all_trials`.
   - Switch-latency percentiles computed over the pooled successful samples from all trials (more data than any single trial alone).
   - Write one `BenchmarkResult(name="failover_reliability", metrics={"trials": _OUTAGE_TRIALS, "total_requests_all_trials": ..., "overall_reliability_pct": ..., "failover_switch_p50_seconds": ..., "failover_switch_p95_seconds": ..., "failover_switch_p99_seconds": ..., "per_trial": [...] , ...}, thresholds={"reliability_min_pct": thresholds.FAILOVER_RELIABILITY_MIN_PCT, "switch_max_seconds": thresholds.FAILOVER_SWITCH_MAX_SECONDS}, passed=...)`. Keep the result `name` as `"failover_reliability"` (unchanged) so it continues to overwrite the same file `run_all.py`/README already reference.
   - Assert `overall_reliability_pct >= thresholds.FAILOVER_RELIABILITY_MIN_PCT` and `failover_switch_p95_seconds < thresholds.FAILOVER_SWITCH_MAX_SECONDS` against the pooled/aggregate numbers (a stronger signal than the old single-trial assertion).

5. Leave `test_circuit_breaker_recovery_latency` completely untouched — out of scope for this phase.

6. Update the module docstring's step-3 reference to note the reliability test now runs multiple outage trials, and update `README.md`'s failover bullet to mention it reports the aggregate request count behind the reliability percentage.

## Acceptance Criteria

```bash
docker compose -f deploy/docker-compose.yml up -d --build
python3 scripts/setup_demo_teams.py
uv run pytest benchmarks/bench_failover.py -m benchmark -v
ruff check benchmarks/ README.md
```

Confirm no residual Redis breaker keys or `circuit_breaker_history` rows remain for `anthropic` after the run (same check the prior phase's step 3 did) — trial-to-trial resets plus the fixture's final teardown must leave clean state.

## Verification Procedure

1. Run the AC commands above.
2. Inspect `benchmarks/results/failover_reliability.json` — confirm `total_requests_all_trials` equals `_OUTAGE_TRIALS * 20` and `per_trial` has exactly `_OUTAGE_TRIALS` entries.
3. Confirm `test_circuit_breaker_recovery_latency` still passes unchanged (`benchmarks/results/failover_recovery.json` still gets written).
4. Confirm the CRITICAL retry/fallback rule in CLAUDE.md (retry primary up to 3x on retryable errors, fall back immediately on non-retryable) isn't affected — this step only changes the benchmark harness, not `gateway/` code.
5. Based on the result, update this step's entry in `phases/benchmark-scale/index.json`:
   - Success → `"status": "completed"`, `"summary": "one-line summary of the output"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't skip the breaker reset between trials. Reason: a breaker left open (or half-open) from a prior trial changes how the next trial's requests are handled (no retry against primary at all vs. retry-then-fallback), corrupting both the reliability and switch-latency numbers for a reason that has nothing to do with the thing being measured.
- Don't touch `test_circuit_breaker_recovery_latency`. Reason: explicitly out of scope per this phase's direction.
- Don't change `benchmarks/common.py`. Reason: shared-contract concern, same as steps 0-1.
- Don't break existing tests.
