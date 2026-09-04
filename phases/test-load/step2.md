# Step 2: integration-resilience

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 5 ("fallback activation, circuit breaker open/close, streaming integrity — using mocked providers with fault injection. Fault injection is stateless and per-request (`X-Mock-Fault` header or magic model name, ADR-025), so concurrent tests/requests each control their own simulated outcome independently.")
- `/docs/TRD.md` — §10 ("Circuit breaker: opens/closes correctly under injected faults", "Streaming: passes through without corruption under load")
- `/docs/ADR.md` — ADR-025 (mock fault injection: header **or magic model-name suffix**, specifically designed so concurrent requests each get independently-controlled behavior)
- `gateway/config/loader.py` — `FallbackChainsConfig`: **only two fixed fields, `fast_tier`/`frontier_tier`, both plain `list[str]`.** This is a Pydantic model with fixed attributes, not a free-form dict — you cannot add a new tier name; you can only append entries to the existing `fast_tier`/`frontier_tier` lists.
- `gateway/providers/registry.py` — `resolve_provider_for_model` does an *exact string match* against `config.providers.<name>.models`. This means: for a magic-suffixed model like `claude-sonnet--fault-error` to be requestable as the **primary** model at all, it must appear verbatim in `providers.anthropic.models` in the config file your tests load. (Fallback *candidates*, by contrast, are looked up via `get_adapter_for_provider` by provider name only — they do **not** need to appear in any `models` list. So only the primary's magic-suffixed model needs a config entry.)
- `gateway/resilience/fallback.py` — `resolve_fallback_chain`: finds a `"{provider}:{model}"` entry's position in whichever tier list contains it and returns everything *after* it. Appending a new `provider:magic-model` entry followed by the same real downstream entries to the end of `fast_tier` gives that magic model its own fallback chain without touching the existing entries other tests already depend on.
- `mocks/fault_injection.py` — `FAULT_TYPES = ("timeout", "error", "rate_limit")`, `_MAGIC_SUFFIX_PREFIX = "--fault-"`. Use `error` for these tests: it returns HTTP 500 immediately (no sleep, unlike `timeout`'s real 30-second `asyncio.sleep`), and `gateway/providers/errors.py`'s `raise_for_provider_status` classifies any status outside `{400, 401, 403, 429}` as `RetryableProviderError` — so a 500 is retryable, exactly what's needed to drive retries → fallback → circuit-breaker failures.
- `gateway/resilience/orchestrator.py` — `_PRIMARY_MAX_ATTEMPTS = 3`, `_RETRY_WAIT_MULTIPLIER_SECONDS = 0.1`, `_RETRY_WAIT_MAX_SECONDS = 2.0` (tenacity exponential backoff) — so each failing primary attempt costs a few hundred ms total, not seconds; size concurrency counts accordingly so the test suite stays fast.
- `gateway/resilience/circuit_breaker.py` — `check_breaker`/`record_failure`/`record_success`, and `tests/test_circuit_breaker.py`'s `test_concurrent_check_breaker_only_one_probe_winner` for the existing direct (non-HTTP) concurrency test pattern for the breaker — this step's job is different: driving the breaker open/half-open/closed cycle through real concurrent HTTP traffic, not direct function calls.
- `tests/fixtures/test_config.yaml` — current `providers`/`fallback_chains`/`circuit_breaker` blocks (`failure_threshold: 5, window_seconds: 60, cooldown_seconds: 30`) — you're editing this file (see Task §1).
- `tests/test_streaming.py` — `_read_sse`/`_assemble_content` helpers and the streaming `client` fixture pattern — reuse both for this step's concurrent-streaming test.
- `tests/test_routing.py` — the comment on why `X-Mock-Fault` (the header) is a dead end for route-level fault testing (`openai_adapter.py`/`anthropic_adapter.py` never forward it) — irrelevant confusion to avoid: this step uses the **magic model-name suffix**, which travels through the request body as the `model` field and *does* reach the mock, unlike the header.

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

### 1. Edit `tests/fixtures/test_config.yaml`

Two additive, surgical changes — don't reorder or remove any existing entry, every other test file's fixtures depend on the current shape:

- Under `providers.anthropic.models`, append one entry: `claude-sonnet--fault-error`.
- Under `fallback_chains.fast_tier`, append three entries to the end of the existing list: `anthropic:claude-sonnet--fault-error, openai:gpt-4o-mini, ollama:llama3` (the same two real downstream candidates the existing `claude-sonnet` entry already falls back to). The result should read:
  ```yaml
  fallback_chains:
    fast_tier: [anthropic:claude-sonnet, openai:gpt-4o-mini, ollama:llama3, anthropic:claude-sonnet--fault-error, openai:gpt-4o-mini, ollama:llama3]
  ```

This makes `claude-sonnet--fault-error` a fully routable primary model (resolves via `resolve_provider_for_model`) whose every real attempt fails with a retryable 500, and which has its own fallback chain to `gpt-4o-mini` then `llama3`.

### 2. Create `tests/integration/test_concurrent_resilience.py`

Local `client` fixture (same `httpx.ASGITransport` pattern as every other route-level test file) and a local `_insert_team`/`_delete_team` helper pair (`allowed_models` must include both `claude-sonnet--fault-error` and `gpt-4o-mini` — team-allowed-models is checked verbatim against the requested `model` field in `routes.py`'s `_prepare_request`, so the magic-suffixed name itself must be allowed, not just its plain counterpart).

**Concurrent fallback activation:** Fire `N` concurrent (`asyncio.gather`) non-streaming requests with `model="claude-sonnet--fault-error"`. Assert every single one returns `200` with `model == "gpt-4o-mini"` in the response body (all served by the fallback, none leak a `503`) — this proves retry-then-fallback doesn't race or cross-contaminate state between concurrent requests sharing the same primary provider's circuit breaker.

**Concurrent circuit-breaker open under real failures:** Use a *provider* that has no fallback chain of its own for the model you drive it with, so failures are forced all the way to exhaustion instead of being absorbed by a fallback — the simplest option is to use a fresh, uniquely-named test-only provider... **don't invent a new provider**; instead, drive `claude-sonnet--fault-error` concurrently in high enough volume (`window_seconds: 60`, `failure_threshold: 5` in `test_config.yaml` — comfortably exceed 5 failed primary attempts against `anthropic` within the window) and assert, via `circuit_breaker_history` (real Postgres, same table `tests/test_circuit_breaker.py` already asserts against), that a `closed → open` transition row exists for `provider = "anthropic"` with `reason = "failure_threshold_reached"`. Then assert that a *subsequent* request with `model="claude-sonnet--fault-error"` still returns `200` (served by fallback) but does **not** touch `anthropic` at all now that its breaker is open — you can't observe "not touched" via HTTP directly, so instead assert no *new* `circuit_breaker_history` row for `anthropic` appears in the few seconds after the breaker opened (the skip path doesn't call `record_failure`, so no new history rows should be written until cooldown elapses). Finally, `asyncio.sleep` past `cooldown_seconds` (30s — accept this real wall-clock wait, same as `test_circuit_breaker.py` already does at a smaller scale) and confirm a subsequent single request produces an `open → half_open` (and, since `claude-sonnet--fault-error` always fails, likely `half_open → open` again) row.
- Clean up the `anthropic` breaker's Redis keys and `circuit_breaker_history` rows in a fixture teardown (same technique as `tests/test_circuit_breaker.py`'s `provider` fixture) — **this test mutates the shared `anthropic` provider's breaker state**, so it must leave things exactly as it found them, or every other test file that touches `anthropic`'s breaker (there are several) becomes order-dependent and flaky.

**Concurrent streaming integrity:** Fire `N` concurrent *streaming* requests (mix of `model="gpt-4o-mini"` and `model="claude-sonnet"`, real working models, no fault injection here — this test is about concurrency safety, not resilience) via `client.stream(...)`, each with distinctive prompt content. For each response, reuse `test_streaming.py`'s `_read_sse`/`_assemble_content` helpers and assert: the SSE stream is well-formed (ends in `[DONE]`), every chunk's `model` field matches what that specific request asked for (proves no cross-request state leakage between concurrent streams), and the assembled content is non-empty and internally consistent (no interleaved fragments from a different concurrent request).

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d redis postgres mock-openai mock-anthropic
uv run pytest tests/integration/test_concurrent_resilience.py -v
uv run pytest -v   # full existing suite must still pass unchanged, including tests/test_circuit_breaker.py and tests/test_fallback.py against the edited fixture
docker compose -f deploy/docker-compose.yml down
```

Note the circuit-breaker portion of this test genuinely waits out a real `cooldown_seconds` (30s) — this is consistent with how `tests/test_circuit_breaker.py` already tests cooldown/half-open behavior (at 1-2s in that file's own configs), just at the fixed 30s value `test_config.yaml` already uses elsewhere. Don't reduce `test_config.yaml`'s shared `cooldown_seconds` to speed this test up — that value is read by every other test file too.

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does `tests/fixtures/test_config.yaml`'s `fast_tier` list still contain its original three entries, untouched, before the four new ones?
   - Does the circuit-breaker test clean up `anthropic`'s Redis breaker keys and `circuit_breaker_history` rows in teardown, leaving no residue for other test files?
   - Does the fallback test assert on the *response body's* `model` field (proving which provider actually served it), not just a `200` status code?
3. Based on the result, update `phases/test-load/index.json` step 2:
   - Success → `"status": "completed"`, `"summary": "one-line summary — file created, the test_config.yaml diff (exact new entries), which real provider's breaker state gets exercised and how teardown cleans it up"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't reorder or remove any existing entry in `tests/fixtures/test_config.yaml`. Reason: many other test files (`test_fallback.py`, `test_routing.py`, `test_streaming.py`, etc.) assert exact behavior against the current `fast_tier`/`frontier_tier`/`providers.*.models` contents — only append.
- Don't invent a new `fallback_chains` tier key (e.g. `fast_tier_test`). Reason: `FallbackChainsConfig` is a Pydantic model with exactly two fixed fields (`fast_tier`, `frontier_tier`) — a new key would either be silently dropped or fail validation, and widening the schema is out of scope for a test-fixture change.
- Don't use the `--fault-timeout` magic suffix in this suite. Reason: it triggers a real 30-second `asyncio.sleep` in the mock per request/attempt — with 3 primary retry attempts that's 90+ seconds per test, making the suite unacceptably slow; `--fault-error` (instant 500, still classified `RetryableProviderError`) achieves the same retry/fallback/breaker behavior instantly.
- Don't leave `anthropic`'s circuit breaker open or leave orphaned `circuit_breaker_history` rows after this test file runs. Reason: the breaker state is global per-provider (Redis keys keyed only by provider name, per ADR-007's stateless-instance design) — leaving it open would make any other test file's `anthropic` traffic fail unpredictably depending on test execution order.
- Do not break existing tests.
