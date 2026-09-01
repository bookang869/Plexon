# Step 1: fallback-retry-nonstreaming

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 3 ("Retry primary provider with exponential backoff (up to 3 attempts) before falling back, only for retryable errors... Fallback chains defined per model tier (not per specific model) in global config.")
- `/docs/TRD.md` — §3 steps 6-7 (provider selection consults circuit-breaker state; call+retry then fallback), §5 (`fallback_chains` YAML shape: `fast_tier`/`frontier_tier` lists of `"provider:model"` strings)
- `/docs/ADR.md` — ADR-008 (canonical wire format; per-provider translation), ADR-011 (reuse existing mechanisms over new coordination machinery)
- `CLAUDE.md` (project root) — the CRITICAL rule: "retry the primary provider (exponential backoff, up to 3 attempts) only for retryable errors... fall back immediately on non-retryable errors"
- `phases/resilience/step0.md`'s actual output: `gateway/resilience/circuit_breaker.py` — read the real file, not just the step doc, for the exact signatures of `check_breaker`/`record_success`/`record_failure`/`BreakerDecision`
- `gateway/providers/errors.py` — `RetryableProviderError`, `NonRetryableProviderError`, and the status-code classification. This step's retry/fallback logic switches entirely on which of these two types was raised.
- `gateway/providers/registry.py` — `resolve_provider_for_model`, `_get_adapter`/`_adapters` cache, `UnknownModelError`. You'll add one new function here.
- `gateway/providers/base.py` — `ProviderAdapter` protocol
- `gateway/routes.py` — `_prepare_request` and `create_chat_completion`'s non-streaming branch (the `try: completion = await adapter.chat_completion(...)` block and its except clauses). You're replacing that direct call with the new orchestration; leave the streaming branch alone (step 2's job).
- `gateway/ratelimit/budget.py` — `compute_cost`, `record_spend` — note carefully how `provider_name` currently flows into these from `routes.py`; this step changes what "the serving provider" means.
- `tests/test_streaming.py` — the `_FaultInjectingAdapter` stub-adapter pattern (lines ~173-191). This step's tests use the same technique.
- `tests/conftest.py` — `seeded_team`, `_reset_provider_registry_cache` (the latter matters: this step adds a new adapter-lookup function that uses the same process-lifetime cache)
- `tests/fixtures/test_config.yaml` — the real `fallback_chains` your `resolve_fallback_chain` tests run against

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

Three pieces: a pure fallback-chain resolver, a generic retry+fallback+breaker orchestrator (built to be reused by step 2's streaming path), and the wiring into `routes.py`'s non-streaming branch.

### 1. Fallback chain resolution — `gateway/resilience/fallback.py`

```python
def resolve_fallback_chain(provider: str, model: str, config: GatewayConfig) -> list[tuple[str, str]]:
    """Search every tier in config.fallback_chains for a `"{provider}:{model}"`
    entry. If found, return the remaining entries in that tier's list *after*
    that position, each split into a (provider, model) tuple -- this is the
    ordered fallback candidate list, degrading from where the request already
    is, never back up toward something earlier/pricier in the chain. If the
    provider:model pair isn't listed in any tier, return an empty list (no
    fallback defined for this model -- the caller retries the primary only,
    same as today's behavior)."""
```

**Design decision already made (don't re-litigate):** degrade-from-position, not restart-from-the-top. A request for `openai:gpt-4o-mini` that fails falls back to whatever comes *after* `gpt-4o-mini` in its tier's chain (e.g. `ollama:llama3`) — it never falls back to `anthropic:claude-sonnet` even though that's earlier in the same chain, because that would silently route a request to a pricier model the team never asked for and wasn't budgeted for.

### 2. Adapter lookup by provider name — add to `gateway/providers/registry.py`

```python
def get_adapter_for_provider(provider: str, config: GatewayConfig) -> ProviderAdapter:
    """Like resolve_provider_for_model, but for when the caller already knows
    exactly which provider it wants (fallback candidates come from
    resolve_fallback_chain already split into provider+model, so there's
    nothing to search for). Reuses the same _get_adapter cache."""
```

### 3. The orchestrator — `gateway/resilience/orchestrator.py`

Generic core, parameterized over "how to make one attempt," so step 2 can reuse it for streaming's "fetch the first chunk" operation instead of a full `chat_completion` call:

```python
T = TypeVar("T")

async def resolve_with_resilience(
    primary_provider: str,
    primary_model: str,
    request: ChatCompletionRequest,
    config: GatewayConfig,
    redis: Redis,
    attempt: Callable[[str, ProviderAdapter, ChatCompletionRequest], Awaitable[T]],
) -> tuple[str, str, T]:
    """Walks primary-then-fallback-chain, returns
    (serving_provider, serving_model, attempt_result) from whichever
    candidate succeeded. `attempt(provider_name, adapter, request)` performs
    exactly one call attempt and must raise RetryableProviderError or
    NonRetryableProviderError (from gateway/providers/errors.py) on failure --
    it does not retry internally; retry policy lives entirely in this
    function. Raises the last ProviderError encountered if every candidate
    (primary + all fallbacks) is exhausted or skipped."""


async def call_with_resilience(
    request: ChatCompletionRequest,
    provider: str,
    model: str,
    adapter: ProviderAdapter,
    config: GatewayConfig,
    redis: Redis,
) -> tuple[str, ChatCompletionResponse]:
    """Non-streaming convenience wrapper: attempt = adapter.chat_completion.
    Returns (serving_provider, response) -- routes.py needs the serving
    provider name because it may differ from the originally-requested
    provider once a fallback has served the request."""
```

**Core rules — must not be violated, in order of what happens per candidate (primary first, then each fallback in `resolve_fallback_chain`'s order):**

1. **Check the breaker first, always.** Call `check_breaker(redis, candidate_provider, config)` before attempting anything against that candidate. If `allowed=False`, skip it entirely — zero attempts, move to the next candidate — this is a normal, expected outcome, not a failure to log.
2. **Attempt count depends on role, not just "is this the primary":**
   - Primary, breaker decision `allowed=True, is_probe=False` (normal closed-state traffic): up to 3 attempts via `tenacity`, exponential backoff, retrying only on `RetryableProviderError` — a `NonRetryableProviderError` propagates immediately without retry (per CLAUDE.md's CRITICAL rule).
   - Any candidate where the breaker decision has `is_probe=True` (a half-open probe — this can happen to the primary or, less commonly, a fallback): exactly **1 attempt, no retry**, regardless of whether it's the primary. Retrying during a half-open probe defeats its purpose — it's supposed to be one cautious canary request, not a fresh burst of traffic against a provider that might still be down.
   - Any fallback candidate with `is_probe=False`: exactly **1 attempt, no retry** (per PRD: retry-with-backoff applies to "the primary provider," not fallbacks — retrying every hop multiplies worst-case latency roughly 3x per hop and works against the breaker's purpose of backing off faster as it walks down a chain during a real outage).
3. **Recording the outcome against that candidate's breaker:**
   - Success → `record_success(redis, candidate_provider, decision.is_probe)`. Return immediately with this candidate as the winner.
   - `RetryableProviderError`, attempts exhausted → `record_failure(redis, candidate_provider, decision.is_probe, config)`. Move to the next candidate.
   - `NonRetryableProviderError` → **do not call `record_failure`** (a rejected request tells you nothing about whether the provider's infrastructure is healthy — CLAUDE.md/PRD's non-retryable classification is about the request, not the provider). If this attempt was also the probe (`is_probe=True`), do not call `record_success` either — the probe's outcome is inconclusive, so just let `probe_claimed`'s TTL expire naturally (step 0 already sized it for this) rather than forcing a close/reopen decision from inconclusive information. Move to the next candidate either way.
4. **Building each fallback's request.** A fallback candidate's model is different from what was requested — build its request via `request.model_copy(update={"model": candidate_model})` before calling `attempt`, so the adapter sends the *fallback's* model string, not the originally-requested one. The primary's attempts always use the original, unmodified request.
5. **Exhaustion.** If every candidate is skipped/exhausted, raise the last `ProviderError` encountered (don't invent a new aggregate exception type — `routes.py`'s existing `except RetryableProviderError` / `except NonRetryableProviderError` blocks already produce the right status codes for whichever type comes out last).

### 4. Wire into `routes.py`'s non-streaming branch

Replace the current:
```python
try:
    completion = await adapter.chat_completion(enriched_request)
except RetryableProviderError as exc:
    ...
except NonRetryableProviderError as exc:
    ...
```
with a call to `call_with_resilience(enriched_request, provider_name, enriched_request.model, adapter, config, redis)` inside the same try/except shape (the except blocks themselves don't need to change — they already handle both error types correctly, now representing "the whole chain failed" instead of just "the primary failed").

**Critical correctness point:** `call_with_resilience` returns `(serving_provider, completion)`. From this point on, `_record_spend`'s call to `compute_cost`/`record_spend` **must use `serving_provider`, not the `provider_name` that `_prepare_request` originally resolved.** If a fallback served the request, pricing and the spend ledger must reflect the provider that actually served it — using the wrong provider's pricing table would either silently misprice the response or raise `compute_cost`'s `ValueError` for a provider/model combination that was never actually called.

Get a `Redis` handle via `gateway.redis_client.get_redis()` and the config via the existing `get_config()` call already in `_prepare_request`.

### 5. Add `tenacity` to `pyproject.toml`

It's referenced in `docs/TRD.md`'s tech stack table (§1) but not yet in `dependencies`. Add it, then `uv sync` (or `uv lock` + `uv sync`) so `uv.lock` picks it up.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d redis postgres
uv run pytest tests/test_fallback.py tests/test_orchestrator.py tests/test_routing.py -v
docker compose -f deploy/docker-compose.yml down
```

`tests/test_fallback.py` (pure function, no I/O):
- A `provider:model` present mid-chain returns only the entries after it, in order.
- A `provider:model` present in `frontier_tier` doesn't pick up `fast_tier` entries or vice versa.
- A `provider:model` not present in any chain returns `[]`.
- The last entry in a chain returns `[]` (nothing left to degrade to).

`tests/test_orchestrator.py` (real Redis for breaker state; stub `ProviderAdapter`-shaped classes for controllable success/`RetryableProviderError`/`NonRetryableProviderError` outcomes per candidate — same technique as `test_streaming.py`'s `_FaultInjectingAdapter`, not the real mock HTTP servers, so attempt counts and timing are exact and fast):
- Primary succeeds on the first attempt → no fallback candidates touched, `record_success` called once against the primary's breaker.
- Primary raises `RetryableProviderError` on every attempt → exactly 3 attempts recorded against the stub before moving on; `record_failure` called once (not 3 times) against the primary's breaker after exhaustion; the request then succeeds against the first fallback.
- Primary raises `NonRetryableProviderError` → exactly 1 attempt (no retry), immediately moves to the first fallback; primary's breaker `failures` count is unaffected (verify via a follow-up `check_breaker` call or by driving it toward `failure_threshold` afterward and confirming the count didn't already include this one).
- Primary's breaker is already open (seed this via step 0's `record_failure` calls directly) → zero attempts against the primary; goes straight to the first fallback candidate.
- Each fallback candidate gets exactly 1 attempt (no retry), tried in `resolve_fallback_chain`'s order, stopping at the first success.
- Every candidate exhausted/skipped → the last error is raised.
- A candidate whose breaker decision is `is_probe=True` gets exactly 1 attempt regardless of whether it's the primary or a fallback; success closes that breaker (verify via a subsequent `check_breaker` call), failure reopens it immediately.
- A probe attempt (`is_probe=True`) that raises `NonRetryableProviderError` calls neither `record_success` nor `record_failure` (verify the breaker is still in whatever transitional state it was in — not forced closed or reopened).

`tests/test_routing.py` — add at least one new test proving the real wiring works end-to-end through `create_chat_completion`: monkeypatch/inject a stub adapter as the resolved primary (e.g. monkeypatch `gateway.providers.registry.resolve_provider_for_model` for the duration of one test, returning a stub that raises `RetryableProviderError`, paired with a `fallback_chains` entry — from the real `test_config.yaml` — whose next candidate is a real, working mock provider) and confirm the route returns `200` with the fallback's response, not a `503`. **Don't use the `X-Mock-Fault` header for this** — `OpenAIAdapter`/`AnthropicAdapter` never forward it to the mock (verify this yourself by re-reading `openai_adapter.py`'s `chat_completion`; it isn't wired through), so it's a dead end for route-level fault testing; a stub adapter is simpler and matches `test_streaming.py`'s existing convention.

Existing tests in `tests/test_routing.py`, `tests/test_priority_tiers.py`, and `tests/test_budget.py` must continue passing unchanged — they exercise the happy path, which now runs through `call_with_resilience` but should behave identically when the primary succeeds on the first attempt.

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Is `resolve_with_resilience` genuinely generic — no `ChatCompletionResponse`-specific logic baked into it, so step 2 can reuse it for a streaming "fetch first chunk" `attempt` function? (`call_with_resilience` is where the non-streaming-specific typing lives.)
   - Does spend/cost recording in `routes.py` use the *serving* provider, not the originally-resolved one?
   - Does the primary ever get more than 1 attempt while `is_probe=True`? (Must not.)
   - Does any fallback candidate ever get retried? (Must not — 1 attempt each.)
3. Based on the result, update `phases/resilience/index.json` step 1:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created, resolve_with_resilience's exact signature (so step 2 can reuse it for streaming), the routes.py serving-provider fix, the tenacity dependency addition"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't retry fallback candidates. Reason: explicitly decided — primary gets 3 attempts, every fallback gets 1, to keep worst-case latency bounded and avoid retry-storming an already-struggling provider during exactly the outage scenario this phase exists to handle well.
- Don't retry during a half-open probe, even for the primary. Reason: a probe is a single cautious canary request by definition; retrying it is a full traffic burst in disguise.
- Don't filter fallback candidates against the requesting team's `allowed_models`. Reason: explicitly decided — `allowed_models` gates what a team can directly request, not the gateway's own resilience degradation path; the whole tier chain is fair game once a request is in flight.
- Don't call `record_failure` for `NonRetryableProviderError`s. Reason: explicitly decided — a rejected request (bad request, auth, content policy) says nothing about provider health; only retry-exhausted `RetryableProviderError`s count toward the breaker's threshold.
- Don't use the `X-Mock-Fault` header in tests expecting it to reach the mock through a real adapter call — it doesn't get forwarded today, and adding that forwarding is out of scope for this step (not asked for; a production `OpenAIAdapter` should never send a test-only fault-injection header). Use stub adapters instead.
- Do not break existing tests.
