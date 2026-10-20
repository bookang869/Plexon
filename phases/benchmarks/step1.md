# Step 1: gateway-overhead-metric

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/TRD.md` — §8 (OTel spans list: `request.receipt` → `auth` → `rate_limit_check` → `provider_selection` → `provider_call` → `response_processing` → `response_delivery`), §10 ("Gateway overhead latency | <10ms")
- `/docs/ADR.md` — ADR-026 (`prometheus-client` direct scrape — new metrics follow the same module-level-constant convention, no wrapper layer)
- `phases/benchmarks/step0.md`'s actual output — `benchmarks/common.py`'s `gateway_client`, `run_concurrent`, `histogram_percentiles_from_metrics_text`, `write_result`/`BenchmarkResult`, and `benchmarks/thresholds.py`'s `OVERHEAD_P95_MS` (or whatever name you gave it) — this step's benchmark is the first consumer of all of these
- `gateway/observability/metrics.py` — the exact style every metric is declared in (module-level `Histogram(...)` constant, no helper wrapper) — `gateway_overhead_seconds` follows the same pattern
- `gateway/routes.py` — `create_chat_completion` in full. Note precisely where `call_started = time.monotonic()` / `elapsed = time.monotonic() - call_started` already exist in **both** the streaming and non-streaming success branches — that `elapsed` is the provider-call duration you need to subtract. You need a wall-clock start captured at the very top of the function (before the `rate_limit_check` span), not reused from anywhere else.
- `tests/test_metrics.py` — the exact fixture/test pattern (`client` fixture via `httpx.ASGITransport`, `seeded_team`, `_sample()` helper reading `prometheus_client.REGISTRY.get_sample_value`) you're extending, not replacing, with new test cases for the new histogram
- `gateway/main.py` — confirms `/metrics` is mounted via `make_asgi_app()`, i.e. reads directly from the same global `prometheus_client.REGISTRY` your new histogram registers into

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

### 1. `gateway/observability/metrics.py`

Add one histogram, same declaration style as the existing ones:

```python
gateway_overhead_seconds = Histogram(
    "gateway_overhead_seconds",
    "Gateway-only processing time (total request time minus the provider-call span), in seconds",
    ["route"],
)
```

### 2. `gateway/routes.py`

At the very top of `create_chat_completion` (before `tier = resolve_tier(...)`), capture `request_started = time.monotonic()`.

In **both** the streaming and non-streaming success paths (the two places that already compute `elapsed = time.monotonic() - call_started` right after a successful `resolve_streaming_start` / `call_with_resilience` call), add one line observing overhead:

```python
overhead = max(0.0, (time.monotonic() - request_started) - elapsed)
gateway_overhead_seconds.labels(route="chat_completion").observe(overhead)
```

Do **not** add this to any error branch (`RetryableProviderError`/`NonRetryableProviderError` handlers) — those paths' `elapsed` already includes retry backoff time that isn't a clean "provider call" duration to subtract, so the overhead split would be misleading there. Only the two success paths get this.

### 3. `benchmarks/bench_overhead.py`

A `@pytest.mark.benchmark` test using `benchmarks/common.py` and the `benchmark_team` fixture from step 0:

```python
@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_gateway_overhead_percentiles(benchmark_team):
    """Fires a handful of warmup requests (discarded, avoids cold-start
    skew), then N non-streaming /v1/chat/completions requests (concurrency
    of your choosing) against a fast mocked model using benchmark_team's
    credentials, against the real running gateway (benchmarks.common.
    gateway_client(), real HTTP -- NOT httpx.ASGITransport, since the
    metric being validated is served over /metrics by the actual running
    process, not an in-process test client). After the load, GETs {GATEWAY_
    BASE_URL}/metrics and calls histogram_percentiles_from_metrics_text(...,
    "gateway_overhead_seconds", {"route": "chat_completion"}) to get P50/
    P95/P99 in seconds. Builds a BenchmarkResult, calls write_result() (so
    the numbers are on disk even if the assertion below fails), then
    asserts p95_ms < thresholds.OVERHEAD_P95_MS."""
```

Requires the real docker-compose stack to be running (see Acceptance Criteria) — this benchmark cannot run against an in-process app, since it validates what the live `/metrics` endpoint actually reports.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d redis postgres mock-openai mock-anthropic
uv run pytest tests/test_metrics.py -v
uv run pytest -v   # full existing suite still passes
docker compose -f deploy/docker-compose.yml down

# benchmark check (separate: needs the full live stack, including the gateway container itself)
docker compose -f deploy/docker-compose.yml up -d --build
uv run python3 scripts/setup_demo_teams.py
uv run pytest benchmarks/bench_overhead.py -m benchmark -v
docker compose -f deploy/docker-compose.yml down
```

Extend `tests/test_metrics.py` with at least one new test asserting `gateway_overhead_seconds_count{route="chat_completion"}` increments by 1 and `gateway_overhead_seconds_sum{route="chat_completion"}` increases by a positive amount after one successful `/v1/chat/completions` request — same `_sample()`/`seeded_team` pattern as the file's existing tests.

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Is `gateway_overhead_seconds` declared exactly like the other metrics in `metrics.py` (module-level constant, no new wrapper/helper function)?
   - Is the overhead observation present in both the streaming and non-streaming success branches, and absent from every error branch?
   - Does `overhead` use `max(0.0, ...)` to guard against a negative value from clock/measurement jitter?
   - Does `bench_overhead.py` hit the real running gateway over HTTP (not `httpx.ASGITransport`)?
3. Based on the result, update `phases/benchmarks/index.json` step 1:
   - Success → `"status": "completed"`, `"summary": "one-line summary — the new metric, exactly which two call sites in routes.py observe it, the new test_metrics.py test(s), bench_overhead.py's measured P95 and whether it passed thresholds.OVERHEAD_P95_MS"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't observe `gateway_overhead_seconds` on any error path. Reason: on a retry/fallback-exhausted path, `elapsed` already includes retry backoff sleeps that aren't part of a clean "one provider call" duration — subtracting it there would produce a number that looks like gateway overhead but isn't, silently corrupting the metric's meaning.
- Don't rename or restructure any existing span (`rate_limit_check`, `provider_selection`, `provider_call`, `response_delivery`) or existing metric. Reason: `tests/test_metrics.py`'s existing tests and this codebase's Grafana dashboards (observability phase) already depend on the current names.
- Don't add a helper/wrapper function around `.observe()` calls. Reason: `metrics.py`'s own module docstring explicitly documents "no wrapper/helper layer, since each call site's available labels differ" — stay consistent with that stated convention.
- Do not break existing tests.
