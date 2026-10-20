# Step 2: throughput-benchmark

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/TRD.md` — §10 ("Concurrent load | 5,000+ concurrent requests")
- `phases/benchmarks/step0.md`'s actual output — `benchmarks/common.py`'s `gateway_client`, `run_concurrent`, `LatencySample`, `percentiles`, `BenchmarkResult`/`write_result`, and `benchmarks/conftest.py`'s `benchmark_team` fixture; `benchmarks/thresholds.py`'s throughput-related constant
- `phases/benchmarks/step1.md`'s actual output — `bench_overhead.py` is the first real consumer of the step-0 harness; follow the same structure (warmup, `gateway_client()`, `write_result` before asserting)
- `tests/load/locustfile.py` — read this in full. This new benchmark is explicitly **not** a replacement for it: Locust drives thousands of real OS-level concurrent users from outside this Python process; this step's benchmark runs tens-to-low-hundreds of `asyncio` tasks inside a single pytest process. Say this distinction in the file's own module docstring so nobody mistakes one for a substitute for the other.
- `mocks/mock_openai/app.py` — confirms the mock's response is near-instant (no artificial delay) — throughput here is bounded by the gateway's own request-handling cost (auth, rate-limit check, budget check, DB/Redis round trips), not by simulated provider latency, which is exactly what a throughput regression benchmark should be sensitive to

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

### `benchmarks/bench_throughput.py`

```python
@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_sustained_throughput(benchmark_team):
    """Sustained-load mode: run_concurrent(..., concurrency=CONCURRENCY,
    duration_seconds=DURATION_SECONDS) firing non-streaming
    /v1/chat/completions requests against benchmark_team's credentials and
    a fast mocked model, against the real running gateway
    (benchmarks.common.gateway_client()). CONCURRENCY and DURATION_SECONDS
    read from env vars (PLEXON_BENCHMARK_CONCURRENCY, PLEXON_BENCHMARK_
    DURATION_SECONDS) with small smoke-test defaults (document the env-var
    override in the module docstring, same convention as locustfile.py's
    "for a quick smoke run... use much smaller -u/-r" note). Computes
    achieved_rps = count(successful samples) / actual_wall_clock_seconds
    (not duration_seconds -- the loop's real elapsed time may exceed the
    requested duration slightly). Builds a BenchmarkResult (rps, error
    count/rate, and latency percentiles via common.percentiles over each
    sample's elapsed_seconds), write_result()s it, then asserts
    achieved_rps >= thresholds.THROUGHPUT_MIN_RPS and error_rate is 0 (any
    failed request at benchmark_team's generous rpm/tpm/budget limits is a
    genuine regression, not expected contention)."""
```

Give `benchmark_team` (step 0) enough rpm/tpm/budget headroom that this benchmark measures raw throughput, not incidental rate-limit/budget rejection — if step 0's fixture isn't generous enough for the concurrency level you choose here, that's a signal to revisit step 0's fixture values, not to work around it in this file.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build
uv run python3 scripts/setup_demo_teams.py
uv run pytest benchmarks/bench_throughput.py -m benchmark -v
docker compose -f deploy/docker-compose.yml down
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does the module docstring clearly distinguish this benchmark's scale/purpose from `tests/load/locustfile.py`'s, rather than implying it satisfies TRD §10's "5,000+ concurrent requests" NFR?
   - Does `achieved_rps` divide by actual measured wall-clock time, not the requested `duration_seconds`?
   - Is concurrency/duration overridable via env vars with a documented smoke-test default?
3. Based on the result, update `phases/benchmarks/index.json` step 2:
   - Success → `"status": "completed"`, `"summary": "one-line summary — file created, default concurrency/duration used, measured RPS and whether it passed thresholds.THROUGHPUT_MIN_RPS"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't claim or imply this benchmark validates TRD §10's "5,000+ concurrent requests" target. Reason: that NFR is Locust's job (a separate, already-built, multi-process load generator); a single pytest process's `asyncio` concurrency is a different, smaller-scale measurement — conflating the two would misrepresent what was actually tested.
- Don't hardcode a specific rpm/tpm/budget value inline in this file that duplicates step 0's `benchmark_team` fixture. Reason: if throughput numbers look artificially low later, the fixture's limits are the first thing to check — keeping them defined in exactly one place (step 0's conftest) avoids two sources of truth drifting apart.
- Do not break existing tests.
