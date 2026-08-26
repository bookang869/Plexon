# Step 2: mock-providers

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ADR.md` — read ADR-006 (mocked OpenAI/Anthropic, real Ollama) and ADR-025 (mock fault injection — stateless, per-request trigger) closely; these define this step's entire contract
- `/docs/TRD.md` — §5's "Mock fault injection (ADR-025)" note (the `X-Mock-Fault` header contract), §4.1's `spend_ledger`/pricing context for why fabricated usage numbers need to be plausible
- `/docs/ARCHITECTURE.md` — "Request Metadata" section, which restates the mock statelessness requirement
- `phases/proxy-layer/step0.md` and step 1's actual output: `gateway/schemas.py` (the canonical models this step's mocks must produce), `deploy/docker-compose.yml` (the `mock-openai`/`mock-anthropic` service stubs step 0 left for you to fill in — read the compose file to see exactly what build context/Dockerfile paths it expects)

## Task

Build two small standalone FastAPI apps under `mocks/` that stand in for real OpenAI and Anthropic APIs. They are **not** gateway code — they don't import from `gateway/` — but `mock-openai`'s response shape should already match `gateway/schemas.py`'s `ChatCompletionResponse`/`ChatCompletionChunk` (since it's mocking a provider that already speaks near-native OpenAI format), while `mock-anthropic` should use Anthropic's actual native shape (since the gateway's `AnthropicAdapter`, built in step 3, is the one responsible for translating it — the mock exists to be realistic, not to pre-translate for the gateway).

### `mocks/mock_openai/app.py`

- `POST /v1/chat/completions` — accepts an OpenAI-shaped request body (`{model, messages, stream, ...}`), returns a `ChatCompletionResponse`-shaped JSON body (non-streaming) or an SSE stream of `ChatCompletionChunk`-shaped `data: ...` lines terminated by `data: [DONE]` (streaming, when `"stream": true`).
- Response content is fabricated (e.g. echo back a fixed or lightly-templated reply referencing the last user message) — there's no real model behind this. Fabricate a plausible `usage` object (ADR-019) — token counts should roughly correlate with input/output length (e.g. `len(text.split())`), not be hardcoded constants, so downstream cost/budget logic (later phases) sees varying numbers.

### `mocks/mock_anthropic/app.py`

- `POST /v1/messages` — accepts an Anthropic-native request body (`{model, messages, max_tokens, stream, ...}`, note Anthropic's `system` is a top-level field, not a `role: system` message — reflect that), returns Anthropic's native response shape (`{id, type: "message", role: "assistant", content: [{type: "text", text: ...}], model, stop_reason, usage: {input_tokens, output_tokens}}`) non-streaming, or Anthropic's native SSE event stream (`message_start`, `content_block_delta` events, etc. — a reasonably faithful subset is fine, it doesn't need every event type Anthropic's real API emits) when streaming.

### Fault injection (ADR-025) — shared by both mocks

Both apps must support a **stateless, per-request** fault trigger, checked before generating any normal response:
- `X-Mock-Fault: timeout` → the handler sleeps well past any reasonable client timeout (e.g. 30s) before returning, simulating a hung provider.
- `X-Mock-Fault: error` → return an HTTP 500 with a generic error body.
- `X-Mock-Fault: rate_limit` → return an HTTP 429 with a `Retry-After` header.
- Also support the same three behaviors via a magic model-name suffix (e.g. requesting model `"gpt-4o-mini--fault-error"`) as an alternative trigger, for callers who can't set custom headers (e.g. some load-test setups) — strip the suffix before using the rest of the model name for response fabrication.
- No global toggle, no shared mutable state, no request counter — every request's behavior is fully determined by its own header/model-name. Concurrent requests with different fault triggers must not interfere with each other.

Write this fault-check as one small shared helper (e.g. `mocks/fault_injection.py`, imported by both apps) rather than duplicating the logic — this is genuinely shared, not premature abstraction, since ADR-025's contract must be byte-identical across both mocks.

### Dockerfiles + compose wiring

- `mocks/Dockerfile.openai` and `mocks/Dockerfile.anthropic` (or one shared `mocks/Dockerfile` with an `ENTRYPOINT`/`CMD` selecting the app via a build arg — your choice, but both services must build and run independently).
- These must satisfy the build context/paths that `deploy/docker-compose.yml`'s `mock-openai`/`mock-anthropic` services (written in step 0) already reference. If step 0's compose file used different paths than you're about to create, update the compose service definitions to match what you actually built — don't contort your directory layout to match a stub that was written before this step existed.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build mock-openai mock-anthropic
curl -sf -X POST http://localhost:<mock-openai-port>/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'   # 200, ChatCompletionResponse shape
curl -s -X POST http://localhost:<mock-openai-port>/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-Mock-Fault: rate_limit' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}' -i   # 429 + Retry-After
curl -sf -X POST http://localhost:<mock-anthropic-port>/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet","max_tokens":100,"messages":[{"role":"user","content":"hi"}]}'   # 200, Anthropic-native shape
docker compose -f deploy/docker-compose.yml down
uv run pytest tests/test_mocks.py -v   # write tests using httpx.AsyncClient / FastAPI TestClient against both apps in-process, covering: normal response, streaming response, all three fault triggers via header, all three via magic model suffix
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Is `mock-openai`'s response shape actually consistent with `gateway/schemas.py` (step 1)?
   - Is `mock-anthropic`'s response shape Anthropic's real native format, not pre-translated to OpenAI shape (that translation is step 3's job, not this step's)?
   - Is the fault-injection helper stateless — no module-level mutable counters/toggles shared across requests?
   - Does `deploy/docker-compose.yml` still build and start `mock-openai`/`mock-anthropic` cleanly?
3. Based on the result, update `phases/proxy-layer/index.json` step 2:
   - Success → `"status": "completed"`, `"summary": "one-line summary — mock ports/paths, fault-trigger contract, for adapter step to build on"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't have `mock-anthropic` return OpenAI-shaped responses. Reason: the point of a faithful mock is to force the real translation logic (ADR-008) to be exercised in step 3 — pre-translating here would make the adapter step trivial and dishonest about what's actually being tested.
- Don't add a global/shared fault-toggle endpoint (e.g. `POST /mock-control/set-fault`). Reason: ADR-025 explicitly rejects this — it would race under concurrent load-test traffic.
- Don't import anything from `gateway/` into `mocks/`. Reason: mocks simulate external third-party services and must stay decoupled from gateway internals.
- Do not break existing tests.
