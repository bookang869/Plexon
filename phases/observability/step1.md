# Step 1: prometheus-metrics

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 4 ("Prometheus metrics: RPS by team/model/provider, error rate by team/model/provider/error-type, latency P50/P95/P99 by provider, token throughput, cost per team per day, fallback trigger rate, circuit-breaker state changes")
- `/docs/TRD.md` — §8 (the exact 8 metric names/labels — treat this list as the literal spec, not a paraphrase), §3 (request-flow step numbering), §9 (deployment services — `prometheus`)
- `/docs/ADR.md` — read the full file for context; this step resolves an ambiguity the tech-stack table leaves open (Prometheus via `prometheus-client` vs an OTel metrics exporter) — see the design decision below, and add a new ADR entry for it (numbered after the last existing entry — check the file for the current highest number)
- `phases/observability/step0.md`'s actual output: `gateway/observability/tracing.py`, and the span instrumentation added to `gateway/main.py`/`gateway/auth/team_auth.py`/`gateway/routes.py` — read the real diff/files, not this description, to see exactly where `provider_call` timing is already measured, since this step's latency histogram reuses that same measurement window
- `gateway/routes.py` — `create_chat_completion`, `_prepare_request`, `_record_spend` (the closure that already has `team`, `provider`, `completed.model`, `usage`, `cost` in scope — the natural place to add most of this step's counters)
- `gateway/resilience/orchestrator.py` — `resolve_with_resilience`'s candidate loop: the exact point where `index > 0` and a candidate succeeds is where a fallback actually served the request
- `gateway/resilience/circuit_breaker.py` — `_record_transition` (the one function every state change already flows through — the natural hook for the two circuit-breaker metrics)
- `gateway/providers/errors.py` — `RetryableProviderError`/`NonRetryableProviderError`, the two exception types you'll classify into `error_type` labels
- `gateway/main.py` — where you'll mount the `/metrics` endpoint
- `pyproject.toml`, `deploy/docker-compose.yml` — current deps/services

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

**Design decision already made: `prometheus-client` with a direct `/metrics` scrape endpoint, not an OTel metrics exporter.** TRD §9's deployment service list is `gateway, redis, postgres, prometheus, grafana, tempo, mock-openai, mock-anthropic, ollama, setup` — no OTel Collector. An OTel metrics pipeline would need one (to fan out OTLP to a Prometheus-scrapeable form), which isn't in the spec. `prometheus-client` exposing metrics directly matches the deployment list as written and needs no new service. This is a real decision, not an obvious reading of the tech-stack table — add a short new ADR entry recording it (append to `docs/ADR.md`, next sequential number, following the existing entries' Context/Decision/Consequences format).

**Design decision already made: metric objects live in one module, incremented directly at each call site — no wrapper/helper function.** `gateway/observability/metrics.py` should define the 8 metric objects below as module-level constants; other modules import and call `.labels(...).inc()` / `.observe()` / `.set()` directly where the relevant data is already in scope. Don't add a `record_request(...)`-style indirection layer — each call site's context (what's known, what labels apply) is different enough that a shared wrapper would just be a pass-through with an `if` for whichever labels aren't known yet.

### 1. Metric definitions — `gateway/observability/metrics.py`

```python
from prometheus_client import Counter, Gauge, Histogram

gateway_requests_total: Counter          # labels: team, model, provider
gateway_errors_total: Counter            # labels: team, model, provider, error_type
gateway_latency_seconds: Histogram       # labels: provider
gateway_tokens_total: Counter            # labels: team, direction   (direction: "input" | "output")
gateway_cost_usd_total: Counter          # labels: team
gateway_fallback_triggered_total: Counter    # labels: from_provider, to_provider
gateway_circuit_breaker_state: Gauge         # labels: provider   (0=closed, 1=half_open, 2=open, per TRD §8)
gateway_circuit_breaker_transitions_total: Counter  # labels: provider, from_state, to_state
```

Use TRD §8's exact metric names verbatim (Grafana dashboards in step 3 will query these by name — a mismatch there is a silent breakage two steps from now).

### 2. `/metrics` endpoint — `gateway/main.py`

```python
from prometheus_client import make_asgi_app
app.mount("/metrics", make_asgi_app())
```

### 3. Wiring into request handling — `gateway/routes.py`

In `_record_spend` (success path, both streaming's `_on_complete` and the non-streaming return path already call it): increment `gateway_requests_total`, `gateway_tokens_total` (once for `usage.prompt_tokens` with `direction="input"`, once for `usage.completion_tokens` with `direction="output"`), `gateway_cost_usd_total`. Use the `serving_provider` parameter already passed in — not the originally-requested provider — matching the resilience phase's existing convention for where cost gets attributed.

In each of the four `RetryableProviderError`/`NonRetryableProviderError` `except` blocks in `create_chat_completion` (streaming and non-streaming branches each have one of each): increment `gateway_requests_total` (labeled with `provider_name`, the originally-resolved primary provider — there's no serving provider on total failure) and `gateway_errors_total` with `error_type="retryable"` or `error_type="non_retryable"` matching which exception was caught.

Measure `gateway_latency_seconds` around the same `call_with_resilience`/`resolve_streaming_start` call that step 0's `provider_call` span already wraps — reuse that timing window rather than adding a second `time.monotonic()` pair. Observe it once the call resolves (success or failure), labeled by whichever provider actually served/was attempted.

### 4. Wiring into the circuit breaker — `gateway/resilience/circuit_breaker.py`

Inside `_record_transition`, after (or alongside) the existing Postgres history write: `gateway_circuit_breaker_transitions_total.labels(provider=provider, from_state=from_state.value, to_state=to_state.value).inc()`, and `gateway_circuit_breaker_state.labels(provider=provider).set(state_to_int(to_state))` using the 0/1/2 mapping from TRD §8. This must run even if the Postgres write fails (the existing `try/except` already isolates that) — a metrics update should never depend on the best-effort history write succeeding.

### 5. Wiring into fallback — `gateway/resilience/orchestrator.py`

In `resolve_with_resilience`'s candidate loop, at the point a candidate with `index > 0` succeeds (right before `await record_success(...)` / `return provider, model, result`), increment `gateway_fallback_triggered_total.labels(from_provider=primary_provider, to_provider=provider).inc()`.

### 6. Deployment wiring

Add a `deploy/prometheus/prometheus.yml` scrape config (one job, target `gateway:8000`, scrape path `/metrics`, a reasonable interval like `15s`). Add a `prometheus` service to `deploy/docker-compose.yml` (image `prom/prometheus:latest`, mount the config, port `9090`, `depends_on: [gateway]`). Add `prometheus-client` to `pyproject.toml`'s `dependencies` (`uv lock && uv sync`).

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d redis postgres mock-openai mock-anthropic
uv run pytest tests/test_metrics.py -v
docker compose -f deploy/docker-compose.yml down
```

`tests/test_metrics.py` — drive real requests through the FastAPI app (same `httpx.AsyncClient` + `seeded_team` pattern as `tests/test_routing.py`), then assert against `prometheus_client.REGISTRY` (e.g. `REGISTRY.get_sample_value("gateway_requests_total", {"team": ..., "model": ..., "provider": ...})`):
- A successful non-streaming request increments `gateway_requests_total`, `gateway_tokens_total` (both directions), `gateway_cost_usd_total` with correct label values.
- A request served by a fallback (reuse `tests/test_fallback.py`'s fault-injection pattern) increments `gateway_fallback_triggered_total{from_provider=X, to_provider=Y}` and attributes `gateway_cost_usd_total`/`gateway_requests_total` to the *serving* provider, not the originally-requested one.
- A request that exhausts every candidate increments `gateway_errors_total` with the correct `error_type`.
- Forcing a circuit breaker to open (reuse `tests/test_circuit_breaker.py`'s `_open_breaker` helper) increments `gateway_circuit_breaker_transitions_total{from_state="closed", to_state="open"}` and sets `gateway_circuit_breaker_state` to `2`.
- `GET /metrics` returns `200` with `Content-Type` starting `text/plain` and contains the string `gateway_requests_total` (a smoke test that the endpoint is actually mounted and serving real registry output).

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Do all 8 metric names/label sets match TRD §8 exactly?
   - Does `gateway_cost_usd_total`/`gateway_requests_total`/token metrics attribute to the *serving* provider on a fallback, not the originally-requested one (same rule the resilience phase already established for the spend ledger)?
   - Is the new ADR entry present in `docs/ADR.md` with the correct next sequential number?
3. Based on the result, update `phases/observability/index.json` step 1:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created/modified, the prometheus-client-not-otel-metrics decision (+ ADR number), exact hook points for each of the 8 metrics"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't introduce an OTel Collector service or route metrics through OTLP. Reason: explicitly decided above — no collector exists in TRD §9's deployment list, and `prometheus-client`'s direct-scrape model needs none.
- Don't add a generic `record_request_metrics(...)` wrapper spanning multiple call sites. Reason: explicitly decided above — each call site has different available data; a shared wrapper adds indirection without removing real duplication.
- Don't change what counts as "serving provider" vs "requested provider" anywhere — reuse the existing distinction from the resilience phase's `serving_provider` parameter threading, don't reintroduce a parallel notion of it.
- Do not break existing tests.
