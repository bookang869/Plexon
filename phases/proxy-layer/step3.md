# Step 3: provider-adapters

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ADR.md` — read ADR-008 (wire format, translation responsibility), ADR-017 (Ollama adapter built now, live verification deferred — this dictates this step's Ollama testing strategy), ADR-019 (token counting: fabricated in mocks, native via Ollama) closely
- `/docs/TRD.md` — §7 (Provider Adapter Interface — the exact `Protocol` this step implements), §5 (YAML `providers` config shape — `base_url`, `models` per provider)
- `phases/proxy-layer/step1.md` and its actual output: `gateway/schemas.py` (canonical models to translate to/from)
- `phases/proxy-layer/step2.md` and its actual output: `mocks/mock_openai/app.py`, `mocks/mock_anthropic/app.py` — read the actual request/response shapes these apps produce, especially `mock-anthropic`'s native Anthropic format and the `X-Mock-Fault`/magic-suffix fault contract, since `OpenAIAdapter`/`AnthropicAdapter` are these mocks' only callers right now
- `phases/proxy-layer/step0.md`'s actual output: `gateway/config/loader.py` (how to read `providers.<name>.base_url` at runtime)

## Task

Implement `gateway/providers/` — one adapter per provider, all conforming to the same `Protocol`, each responsible for translating between its provider's native format and the canonical schema from step 1.

### `gateway/providers/base.py`

```python
class HealthStatus(BaseModel):
    provider: str
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None

class ProviderAdapter(Protocol):
    async def chat_completion(self, request: ChatCompletionRequest) -> ChatCompletionResponse: ...
    async def chat_completion_stream(self, request: ChatCompletionRequest) -> AsyncIterator[ChatCompletionChunk]: ...
    async def health_check(self) -> HealthStatus: ...
```

This must match `docs/TRD.md` §7's interface exactly (same method names/signatures) — later phases (`resilience`'s health-check loop, `ratelimit-budget`'s provider selection) depend on this exact shape.

### `gateway/providers/openai_adapter.py` — `OpenAIAdapter`

Talks to `mock-openai` (base URL from config). Since `mock-openai` already speaks the canonical schema (per step 2), this adapter is close to a passthrough — but still validate/parse the mock's response into `ChatCompletionResponse` rather than trusting raw JSON, and propagate the mock's fault responses (500 → raise a retryable-marked exception, 429 → raise with the `Retry-After` value attached, per the exception-typing rule below).

### `gateway/providers/anthropic_adapter.py` — `AnthropicAdapter`

Talks to `mock-anthropic`. This one does **real translation** (ADR-008's actual burden):
- Outbound: `ChatCompletionRequest` → Anthropic native (`messages` minus any `role: system` entries, which become the top-level `system` string; `max_tokens` — Anthropic requires it, default to a reasonable value like 1024 if the incoming request didn't set one).
- Inbound: Anthropic's native response (`content: [{type: "text", text}]`, `usage: {input_tokens, output_tokens}`, `stop_reason`) → `ChatCompletionResponse` (`choices[0].message.content` = concatenated text blocks, `usage.prompt_tokens`/`completion_tokens`/`total_tokens` = Anthropic's `input_tokens`/`output_tokens`/their sum, `finish_reason` = a mapped equivalent of `stop_reason`).
- Streaming: Anthropic's native SSE events → `ChatCompletionChunk` stream, mapping content-delta events to `choices[0].delta.content`.

### `gateway/providers/ollama_adapter.py` — `OllamaAdapter`

Talks to a real Ollama server (base URL from config, default `http://ollama:11434` — **not** part of this phase's `docker-compose.yml`, since Ollama isn't installed on the dev machine, per ADR-017). Translate Ollama's native `/api/chat` request/response format, including `prompt_eval_count`/`eval_count` → `usage.prompt_tokens`/`completion_tokens` (ADR-019).

Per ADR-017: this adapter's automated tests must use a stubbed HTTP response (e.g. `httpx.MockTransport` or `respx`) — do not write a test that requires a real running Ollama server, and do not add it to `docker-compose.yml` in this step. Add a one-line note (docstring or comment on the test module) that live-call verification against a real Ollama server is a manual step, deferred per ADR-017.

### Exception typing (used by all three adapters, and by later resilience-phase retry/fallback logic)

Define in `gateway/providers/base.py` (or a sibling `errors.py`):

```python
class ProviderError(Exception): ...
class RetryableProviderError(ProviderError): ...      # timeouts, rate limits (429)
class NonRetryableProviderError(ProviderError): ...    # auth failures, content policy, 4xx other than 429
```

This distinction must not be violated: it's what the `resilience` phase's retry/fallback logic (CLAUDE.md's "retry the primary provider... only for retryable errors... fall back immediately on non-retryable errors") will switch on. Getting this classification wrong here means re-auditing every adapter later — get the exception used for each HTTP status/failure mode right now: timeouts and 429 → `RetryableProviderError`; 401/403/400 (content policy) → `NonRetryableProviderError`; anything else (500, connection refused) → `RetryableProviderError` (transient infra failure, worth a retry).

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build mock-openai mock-anthropic
uv run pytest tests/test_providers.py -v
# tests/test_providers.py must cover:
#   - OpenAIAdapter.chat_completion against the real running mock-openai (non-streaming + streaming)
#   - AnthropicAdapter.chat_completion against the real running mock-anthropic (non-streaming + streaming), asserting correct translation of a multi-message conversation including a system message
#   - AnthropicAdapter / OpenAIAdapter raising RetryableProviderError on X-Mock-Fault: timeout/error, and on rate_limit
#   - OllamaAdapter.chat_completion against a stubbed HTTP transport (no live server) — request/response translation correctness only
docker compose -f deploy/docker-compose.yml down
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Do all three adapters implement the exact `ProviderAdapter` Protocol from `docs/TRD.md` §7?
   - Does `AnthropicAdapter` perform genuine translation (system message hoisting, content-block joining, usage field remapping) rather than passing through canonical-shaped data it was never given?
   - Is the retryable/non-retryable exception split applied consistently across all three adapters?
   - Does `OllamaAdapter`'s test suite avoid any live network call to a real Ollama server?
3. Based on the result, update `phases/proxy-layer/index.json` step 3:
   - Success → `"status": "completed"`, `"summary": "one-line summary — adapter class names/paths, exception types, for the routing/auth steps to use"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't add retry/backoff logic inside the adapters themselves (e.g. wrapping calls in `tenacity`). Reason: retry/backoff is the `resilience` phase's responsibility (CLAUDE.md, TRD §12) — adapters should raise the right exception type and let the caller decide whether to retry.
- Don't add a live Ollama service to `docker-compose.yml` or write a test requiring one. Reason: ADR-017 explicitly defers this to a manual step.
- Don't have `OpenAIAdapter` skip validation/parsing of the mock's response just because the shapes already match. Reason: real OpenAI responses could still differ from the mock in edge cases, and skipping validation here would leave a silent gap if the mock's contract ever drifts from `gateway/schemas.py`.
- Do not break existing tests.
