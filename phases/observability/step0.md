# Step 0: otel-tracing

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 4 ("Observability": OTel traces exported to Grafana Tempo for waterfall visualization)
- `/docs/TRD.md` — §8 ("OTel spans per request: `request.receipt` → `auth` → `rate_limit_check` → `provider_selection` → `provider_call` → `response_processing` → `response_delivery`. Attributes on each: `team_id`, `model_requested`, `model_served`, `input_tokens`, `output_tokens`, `latency_ms`, `cost_usd`"), §9 (deployment services — `tempo`), §3 (full request-flow step numbering, so you can map each named span onto the code that actually does that step)
- `/docs/ADR.md` — ADR-013 (Tempo added to the stack specifically because Prometheus alone has nowhere to store/display individual traces), ADR-006 (test suite must never require real network calls — this governs how you test tracing)
- CLAUDE.md (project root) — the async-native FastAPI stack (ADR-001), Redis/Postgres split (ADR-004), stateless-gateway rule (ADR-007)
- `gateway/main.py` — the `lifespan` context manager and `app = FastAPI(...)` you'll wire tracing into
- `gateway/routes.py` — `create_chat_completion`, `_prepare_request`: this is where rate-limit/budget/enrichment/provider-selection/provider-call/spend-recording all actually happen; you'll thread spans through this function
- `gateway/auth/team_auth.py` — `get_current_team`, the FastAPI dependency that resolves team auth *before* the route handler body runs (relevant to how the `auth` span has to be wired — see design note below)
- `gateway/resilience/orchestrator.py` — `call_with_resilience`, `resolve_streaming_start`: the actual provider-call boundary
- `gateway/streaming.py` — `stream_chat_completion`: where streaming responses are actually sent to the client
- `pyproject.toml` — current dependency list
- `deploy/docker-compose.yml` — current service list (no `tempo` service yet)
- `tests/conftest.py` — env-var-driven config pattern (`PLEXON_DATABASE_URL`, `PLEXON_REDIS_URL`, etc.) — mirror this for any new env var

Read carefully through the code produced in previous phases, understand the design intent, and then start working.

## Task

This is the first step of the `observability` phase. Nothing in this codebase touches OTel yet — `gateway/observability/__init__.py` exists but is empty.

**Design decision already made: spans are wired at the exact point each pipeline stage's code executes, not inferred after the fact.** FastAPI resolves dependencies (like `get_current_team`) *before* the route handler body runs, so the `auth` span can't be opened from inside `create_chat_completion`'s body — it has to nest inside a request-scoped root span that's already current by the time dependency resolution happens. Use a small ASGI/`BaseHTTPMiddleware` in `gateway/main.py` that opens **one root span per request** (name it `request.receipt`, per TRD §8's first stage — it's fine and expected for this span to end up spanning the whole request; the point is it establishes the OTel context, not that it's literally brief) and keeps it current (`with tracer.start_as_current_span(...)`) for the full `call_next(request)` duration. The remaining six spans are opened as **children**, at the point their stage's code actually runs:
- `auth` — inside `get_current_team` (`gateway/auth/team_auth.py`)
- `rate_limit_check` — around the `check_rate_limit` call in `routes.py`
- `provider_selection` — around `_prepare_request`'s enrichment + `resolve_provider_for_model` call
- `provider_call` — around `call_with_resilience` (non-streaming) / `resolve_streaming_start` (streaming)
- `response_processing` — around the post-call bookkeeping (`reconcile_tpm`, `compute_cost`, `record_spend` — i.e. `_record_spend`'s body)
- `response_delivery` — around actually returning the response to the caller: for non-streaming, this can be a short span around the final `return completion`; for streaming, wrap the `StreamingResponse`/`stream_chat_completion` iteration, since that's where response bytes are actually being sent over the wire

**Design decision already made: tracing must never affect request outcomes.** Use `BatchSpanProcessor` (async, non-blocking export) — an unreachable Tempo must never raise into request handling, delay a response, or turn a 200 into a 500. The OTel SDK's own exporter already swallows/logs export failures; do not add your own try/except around span creation to compensate for a design that's already fail-open by default, but do NOT let `configure_tracing()` itself raise if telemetry setup fails in a way that would prevent app startup — wrap the exporter/provider construction so a startup failure logs and no-ops (falls back to `opentelemetry.sdk.trace.export.ConsoleSpanExporter` is NOT required — a `NoOpTracerProvider`-equivalent fallback, i.e. just not configuring OTel further, is enough).

**Design decision already made: attributes are set with whatever's known when each span closes, not blocked on data that arrives later.** `team_id`/`model_requested` are known from the very start; `model_served` is only known once `provider_call` resolves (it may differ from `model_requested` if a fallback served); `input_tokens`/`output_tokens`/`cost_usd` are only known in `response_processing`. Set each attribute on whichever span is current when that value first becomes available — don't thread every attribute onto every span.

### 1. Tracing setup — `gateway/observability/tracing.py`

```python
def configure_tracing(service_name: str = "plexon-gateway") -> None:
    """Sets a global TracerProvider with a BatchSpanProcessor exporting via
    OTLP gRPC to PLEXON_OTEL_EXPORTER_ENDPOINT (env var, default
    "localhost:4317" -- mirrors how PLEXON_REDIS_URL/PLEXON_DATABASE_URL are
    read in gateway/redis_client.py and gateway/db.py). Safe to call once at
    startup; never raises."""

def get_tracer() -> trace.Tracer:
    """opentelemetry.trace.get_tracer(__name__) -- kept as one call site so
    other modules import this instead of opentelemetry.trace directly."""
```

Wire `configure_tracing()` into `gateway/main.py`'s `lifespan`, before the middleware/app handles any requests (call it before `yield`, alongside `init_pool`/`init_redis`).

### 2. Root-span middleware — `gateway/main.py`

Add a small middleware (function-based `@app.middleware("http")` or a `BaseHTTPMiddleware` subclass — your call) that opens the `request.receipt` root span around `call_next(request)`. Set `team_id` on it once auth resolves if easy to do from the middleware layer; otherwise it's fine for `team_id` to only appear on the `auth` span and downstream spans that have access to the resolved `Team`.

### 3. Instrumenting the pipeline — `gateway/auth/team_auth.py`, `gateway/routes.py`

Add the `auth`, `rate_limit_check`, `provider_selection`, `provider_call`, `response_processing`, `response_delivery` child spans at the locations described in the design decision above, using `get_tracer().start_as_current_span(name)` as a context manager. Set attributes per TRD §8 on whichever span first has the data available.

### 4. Dependencies and deployment wiring

Add the OTel packages this step actually uses to `pyproject.toml`'s `dependencies` (SDK, API, and an OTLP gRPC span exporter — check current PyPI package names, e.g. `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`; run `uv lock && uv sync` after editing, matching how `tenacity` was added in the resilience phase).

Add a `tempo` service to `deploy/docker-compose.yml` (image `grafana/tempo:latest`, a minimal local-storage config file at `deploy/tempo/tempo.yaml` mounted in, OTLP gRPC receiver on `4317`, Tempo's own query API on `3200`). Add `PLEXON_OTEL_EXPORTER_ENDPOINT: "tempo:4317"` to the `gateway` service's `environment` block and add `tempo` to its `depends_on`. You do not need to bring this container up for the AC below (tests use `InMemorySpanExporter`), but it must be valid enough for a human to `docker compose up` and see traces land in Tempo later — keep the Tempo config minimal (local disk storage backend is fine; no need for object-storage backends).

## Acceptance Criteria

```bash
uv run ruff check .
uv run pytest tests/test_tracing.py -v
```

`tests/test_tracing.py` — use `opentelemetry.sdk.trace.export.in_memory_span_exporter.InMemorySpanExporter` (per ADR-006: no real Tempo, no network calls in tests) wired into a `TracerProvider` you construct directly in the test (don't call `configure_tracing()`, which reads a real OTLP endpoint env var — build a test-local provider/exporter and monkeypatch/inject it the same way other tests substitute stub adapters). Drive a full request through the FastAPI app (reuse the `httpx.AsyncClient`/`TestClient` + `seeded_team` pattern already used in `tests/test_routing.py` or `tests/test_streaming.py`) and assert:
- All seven span names (`request.receipt`, `auth`, `rate_limit_check`, `provider_selection`, `provider_call`, `response_processing`, `response_delivery`) are present for one successful non-streaming request.
- The six non-root spans are children of the `request.receipt` span (check `parent.span_id` against the root span's `context.span_id`).
- `team_id`, `model_requested`, `model_served`, `input_tokens`, `output_tokens`, `cost_usd` attributes end up set on the expected spans with correct values.
- A request that fails before provider selection (e.g. disallowed model → 403) still produces `request.receipt`/`auth`/`rate_limit_check` spans without crashing on the missing later spans.
- `model_served` differs from `model_requested` when a fallback provider actually served the request (reuse the fault-injection pattern from `tests/test_fallback.py`/`tests/test_orchestrator.py` to force a fallback).

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does `configure_tracing()` fail open (log, don't raise) if the exporter can't be constructed?
   - Is `gateway/observability/tracing.py` the only place that imports `opentelemetry.trace` directly for provider/tracer setup, per ADR pattern of one clear module boundary per concern?
   - Does the span nesting match `request.receipt` as root with the other six as children, not siblings?
3. Based on the result, update `phases/observability/index.json` step 0:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created/modified, the middleware-root-span design, exact span-to-code-stage mapping, which attributes land on which span"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed (e.g. can't determine a working OTLP dependency set) → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't add OTel auto-instrumentation packages (`opentelemetry-instrumentation-fastapi` etc.). Reason: TRD §8 specifies exact named spans mapped to this gateway's own pipeline stages, not generic per-endpoint HTTP spans — manual instrumentation is what the spec actually asks for, and pulling in an auto-instrumentor would produce a second, redundant set of spans.
- Don't let any tracing code raise into request handling or change an HTTP status code/response body. Reason: telemetry is additive — a broken Tempo connection must never turn a working gateway into a broken one (mirrors the same fail-open reasoning already applied to `circuit_breaker_history`/`provider_health_history` best-effort writes in the resilience phase).
- Don't add a YAML config block for the OTLP endpoint. Reason: deployment endpoints (`PLEXON_DATABASE_URL`, `PLEXON_REDIS_URL`, `PLEXON_CONFIG_PATH`) are all env vars in this codebase, never YAML (ADR-005 reserves YAML for global *application* settings, not per-deployment connection strings) — stay consistent, use `PLEXON_OTEL_EXPORTER_ENDPOINT`.
- Do not break existing tests.
