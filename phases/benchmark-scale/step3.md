# Step 3: ratelimit-scale

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ARCHITECTURE.md`
- `/docs/ADR.md`
- `/benchmarks/bench_ratelimit_budget.py` — the existing `test_rpm_admission_accuracy_at_higher_concurrency` (single 200-concurrent wave against `rpm_limit=30`) this step extends, plus its `_insert_team`/`_delete_team` helpers
- `/benchmarks/common.py` — `run_concurrent`, `percentiles`, `BenchmarkResult`, `write_result` (do not modify)
- `/benchmarks/thresholds.py` — `RATELIMIT_REJECTION_P95_MAX_MS` and its calibration comment (measured at 200-concurrent against a 30 rpm_limit; this step repeats that exact shape, so the threshold still applies)
- `gateway/ratelimit/token_bucket.py` — confirms `check_and_consume` is a single atomic Redis EVAL per request, which is why over/under-admission is asserted exactly rather than with a tolerance (see `RATELIMIT_ADMIT_TOLERANCE`'s docstring for the contrast case)

## Task

The current test runs one 200-concurrent wave and asserts exactly 30/200 admitted. That's a single sample of the admission-accuracy claim. Repeat the same wave shape enough times to accumulate roughly 10,000 total requests, so the result can report "0 incorrect admissions across N requests" instead of "correct once."

1. Add a module-level trial count, env-overridable and defaulting so trials × the existing 200-per-wave size lands near 10,000: `_RPM_TRIALS = int(os.environ.get("PLEXON_BENCHMARK_RATELIMIT_TRIALS", "50"))` (50 × 200 = 10,000).

2. Modify `test_rpm_admission_accuracy_at_higher_concurrency` to loop `_RPM_TRIALS` times. Each trial must:
   - Insert a **fresh team** via the existing `_insert_team(db_pool, rpm_limit=_RPM_LIMIT, tpm_limit=100_000_000)` helper (do not reuse one team across trials — the rpm token bucket's window state is keyed per-team in Redis, so back-to-back trials against the same team would measure window-carryover/refill behavior rather than N independent from-cold admission tests).
   - Run the existing 200-concurrent wave against that team with the existing dedicated-connection-pool `httpx.AsyncClient` (recreate it per trial or reuse one client across trials with headers swapped per team — either is fine as long as the raised `max_connections` limit from the current code is preserved).
   - Delete the team via `_delete_team` before moving to the next trial (keeps Postgres/team-API-key tables from accumulating 50 rows per run).
   - Record `{"trial": i, "admitted_count": ..., "rejected_count": ...}` and keep that trial's rejected-sample latencies for pooling.

3. After all trials, compute:
   - `total_requests` = sum across trials (should equal `_RPM_TRIALS * _RPM_TOTAL_REQUESTS`, i.e. ~10,000 by default).
   - `incorrect_admission_count` = sum over trials of `abs(admitted_count - _RPM_LIMIT)` — this counts individual wrong admission decisions (a 31st request wrongly let through, or one of the 30 wrongly rejected), not just "trials that didn't match exactly."
   - `rejection_p50_ms`/`p95_ms`/`p99_ms` computed over the pooled rejected-sample latencies from every trial (more data than the current single-wave estimate).
   - Write one `BenchmarkResult(name="ratelimit_admission_accuracy", metrics={"trials": _RPM_TRIALS, "total_requests": ..., "incorrect_admission_count": ..., "rejection_p50_ms": ..., "rejection_p95_ms": ..., "rejection_p99_ms": ..., "per_trial": [...]}, thresholds={"expected_incorrect_admissions": 0, "rejection_p95_max_ms": thresholds.RATELIMIT_REJECTION_P95_MAX_MS}, passed=...)`. Keep the result `name` unchanged (`"ratelimit_admission_accuracy"`) so it continues to overwrite the file already referenced elsewhere.
   - Assert `incorrect_admission_count == 0` and `rejection_p95_ms < thresholds.RATELIMIT_REJECTION_P95_MAX_MS` against the pooled numbers.

4. Leave `test_budget_overshoot_under_concurrency` completely untouched in this step (it's covered by step 4 of this phase, which is documentation-only and doesn't touch this file's code).

5. Update the module docstring to note the rpm test now runs multiple trials for a larger aggregate sample, and update `README.md`'s rate-limit bullet to mention the ~10,000-request aggregate sample size.

## Acceptance Criteria

```bash
docker compose -f deploy/docker-compose.yml up -d --build
python3 scripts/setup_demo_teams.py
uv run pytest benchmarks/bench_ratelimit_budget.py::test_rpm_admission_accuracy_at_higher_concurrency -m benchmark -v
uv run pytest benchmarks/bench_ratelimit_budget.py -m benchmark -v   # full file, including the untouched budget test
ruff check benchmarks/ README.md
```

Note this will take noticeably longer than before (50 sequential trials of team-insert + 200-concurrent wave + team-delete each) — that's expected and is the point of the larger sample. If it proves impractically slow in your environment, it's fine to leave `_RPM_TRIALS` at a smaller default as long as it's clearly documented and still overridable to reach ~10,000 via the env var — flag this tradeoff in the step's completion summary rather than silently shipping a much smaller default than requested.

## Verification Procedure

1. Run the AC commands above.
2. Inspect `benchmarks/results/ratelimit_admission_accuracy.json` — confirm `total_requests` is close to 10,000 (exactly `_RPM_TRIALS * 200` with defaults) and `incorrect_admission_count` is `0`.
3. Confirm `test_budget_overshoot_under_concurrency` in the same file still passes unchanged.
4. Confirm the CRITICAL rule that per-team rate-limit state lives in Redis, never gateway-process memory (ADR-007), isn't violated — this step only adds more teams/trials using the existing mechanism.
5. Based on the result, update this step's entry in `phases/benchmark-scale/index.json`:
   - Success → `"status": "completed"`, `"summary": "one-line summary of the output"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't reuse one team across trials. Reason: the rpm token bucket's window state is per-team; reusing a team makes later trials measure window-refill/carryover behavior instead of N independent admission tests, silently invalidating the larger sample size this step exists to produce.
- Don't touch `test_budget_overshoot_under_concurrency`. Reason: covered separately by step 4 of this phase (documentation only, no code change).
- Don't change `benchmarks/common.py`. Reason: shared-contract concern, same as prior steps.
- Don't break existing tests.
