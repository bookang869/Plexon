# Step 2: fallback-retry-streaming

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ADR.md` — ADR-009 (streaming: per-provider real-time translation with tee-logging; the "no clean status code left" constraint once the first chunk is sent)
- `/docs/TRD.md` — §3 ("Streaming follows the same steps 1-6, then per-provider stream chunks are translated to OpenAI-style SSE in real time while being teed into a buffer")
- `phases/resilience/step1.md`'s actual output: `gateway/resilience/orchestrator.py` (`resolve_with_resilience`'s exact signature — read the real file, this step reuses it, doesn't reimplement it) and `gateway/resilience/fallback.py`
- `gateway/streaming.py` — read the whole file, especially its module docstring's "Mid-stream faults" section and the `stream_chat_completion` docstring. You are changing this function's signature; understand exactly what it currently does before changing it.
- `gateway/routes.py` — `create_chat_completion`'s streaming branch (the `if enriched_request.stream:` block). You're replacing its direct `stream_chat_completion(adapter, enriched_request, ...)` call.
- `tests/test_streaming.py` — every test that calls `stream_chat_completion` directly (there are several, both with a real `AnthropicAdapter` and with the stub `_FaultInjectingAdapter`). All of them need updating for the signature change below — read them all before starting, not just the fault-handling one.

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

**Design decision already made (don't re-litigate):** retry/fallback applies only to *establishing* a stream (getting the adapter's iterator and its first chunk). Once a chunk has been sent to the caller, no more retry/fallback happens — a fault after that point keeps today's existing behavior exactly (one final SSE chunk carrying an `error` field, then `[DONE]`, `on_complete` not called). This is a direct consequence of ADR-009: once the HTTP response has started (status 200, headers sent), there's no clean status code left to retry into.

### 1. Change `stream_chat_completion`'s signature in `gateway/streaming.py`

It currently takes `(adapter, request, on_complete)` and calls `adapter.chat_completion_stream(request)` itself. That responsibility moves to the caller (the resolver below), because *establishing* the stream is now part of the retry/fallback-eligible window, and by the time this function is called, that resolution has already succeeded. New signature:

```python
async def stream_chat_completion(
    chunks: AsyncIterator[ChatCompletionChunk],
    first_chunk: ChatCompletionChunk,
    request: ChatCompletionRequest,
    on_complete: Callable[[ChatCompletionResponse], Awaitable[None] | None] | None = None,
) -> AsyncIterator[bytes]:
```

`chunks` is an already-in-flight async iterator (from some adapter's `chat_completion_stream`) whose first item has *already* been pulled out as `first_chunk`. This function's job is unchanged otherwise: serialize `first_chunk`, then the rest of `chunks`, to SSE as they arrive; tee everything into the assembled `ChatCompletionResponse` for `on_complete`; catch `ProviderError` from `chunks`' remaining iteration exactly as today (mid-stream fault → one error chunk + `[DONE]`, `on_complete` skipped). The simplest correct change: process `first_chunk` through the same per-chunk logic the loop body already has (accumulate content, check `finish_reason`, yield SSE bytes) before entering the loop over the rest of `chunks` — don't duplicate that logic in two places; restructure so both the pre-fetched first chunk and the loop's subsequent chunks flow through one code path.

### 2. The streaming resolver — add to `gateway/resilience/orchestrator.py`

```python
async def resolve_streaming_start(
    primary_provider: str,
    primary_model: str,
    request: ChatCompletionRequest,
    config: GatewayConfig,
    redis: Redis,
) -> tuple[str, str, AsyncIterator[ChatCompletionChunk], ChatCompletionChunk]:
    """Runs resolve_with_resilience with an `attempt` that opens
    adapter.chat_completion_stream(request) and pulls exactly one item from
    it (via __anext__()) to confirm the stream actually started producing
    output. Returns (serving_provider, serving_model, chunks, first_chunk) --
    `chunks` is the same iterator, already advanced past its first item,
    ready to be handed to stream_chat_completion. Retry/fallback behavior
    (attempt counts, breaker checks, what counts as a failure) is entirely
    resolve_with_resilience's existing logic from step 1 -- this function
    only supplies the streaming-specific `attempt` callable and unwraps its
    (iterator, first_chunk) result type."""
```

A `RetryableProviderError`/`NonRetryableProviderError` raised while opening the connection or pulling the first item propagates out of `attempt` exactly like any other provider call failure — `resolve_with_resilience` already knows how to retry/skip/fall back on it. No new error-handling logic is needed here beyond wiring the callable correctly.

### 3. Wire into `routes.py`'s streaming branch

Call `resolve_streaming_start` *before* constructing the `StreamingResponse` (this is what makes retry/fallback possible here — nothing has been sent to the client yet at this point). If it raises, let it propagate into the same `except RetryableProviderError` / `except NonRetryableProviderError` blocks the non-streaming branch already uses — this is a real behavior improvement worth understanding: today, a streaming request whose *very first* provider call fails still gets a `200` with a single SSE `error` chunk, because nothing checks before starting the response. After this step, a failure that occurs before any chunk is produced (including after exhausting every fallback) now correctly surfaces as `503`/`502`, matching the non-streaming path — only a fault occurring *after* a chunk has already been sent still falls through to the SSE-error-chunk behavior, because at that point a status code genuinely can't change anymore.

**Critical correctness point (same as step 1):** use the `serving_provider`/`serving_model` returned by `resolve_streaming_start` for the `on_complete` callback's `reconcile_tpm`/`compute_cost`/`record_spend` calls — not the originally-resolved `provider_name`, for the same reason as the non-streaming path: a fallback may have served this response, and cost/spend must reflect who actually served it.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d redis postgres
uv run pytest tests/test_streaming.py tests/test_orchestrator.py tests/test_routing.py -v
docker compose -f deploy/docker-compose.yml down
```

Update every existing `tests/test_streaming.py` test that calls `stream_chat_completion` directly to the new signature: pull the first chunk from the adapter's `chat_completion_stream(request)` yourself (`it = adapter.chat_completion_stream(request); first = await it.__anext__()`), then call `stream_chat_completion(it, first, request, on_complete=...)`. This applies to both the real-`AnthropicAdapter` test and the `_FaultInjectingAdapter` mid-stream-fault test — the latter's stub still yields one good chunk then raises on the *second* `__anext__()`, so pulling the first chunk manually in the test still succeeds, and the existing mid-stream-fault assertions (error chunk + `[DONE]`, `on_complete` not called) should be unaffected by this refactor.

New coverage needed:
- `resolve_streaming_start`: primary's stream-open fails with `RetryableProviderError` (stub adapter whose `chat_completion_stream` raises before yielding anything) → falls back to the next candidate, which succeeds; returned `serving_provider` reflects the fallback, not the primary.
- Primary and every fallback fail before yielding a first chunk → the last error propagates out of `resolve_streaming_start` uncaught (matching `resolve_with_resilience`'s documented exhaustion behavior).
- Route-level (`tests/test_routing.py` or `tests/test_streaming.py`, whichever already has route-test infrastructure): a streaming request whose primary is stubbed to fail before any chunk, with a working fallback available, returns `200` and the fallback's SSE content — not a `503` and not a fake-`200`-with-error-chunk.
- Route-level: a streaming request where primary and every fallback fail before any chunk returns `503` (or `502` if the last error was non-retryable) — proving the "no fake 200" improvement described above actually reaches the HTTP layer, not just the resolver function.
- A streaming request served by a fallback records spend/reconciles tpm against the fallback's provider/model, not the originally-requested one (mirrors step 1's equivalent non-streaming test).

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does `stream_chat_completion` still perform the tee/assemble/mid-stream-fault behavior identically to before this step, just fed by a different chunk source?
   - Is `resolve_with_resilience` reused as-is (no duplicated retry/fallback/breaker logic copy-pasted into the streaming path)?
   - Does a pre-first-chunk failure across the whole chain now produce a real HTTP error status, instead of a `200` with a fake success envelope?
3. Based on the result, update `phases/resilience/index.json` step 2:
   - Success → `"status": "completed"`, `"summary": "one-line summary — the stream_chat_completion signature change, resolve_streaming_start, the routes.py wiring, the pre-first-chunk-failure status-code improvement"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't attempt retry/fallback after the first chunk has been sent. Reason: explicitly decided — no clean status code exists at that point (ADR-009); this step only extends resilience to the pre-first-chunk window, mid-stream behavior is unchanged.
- Don't duplicate `resolve_with_resilience`'s retry-count/breaker-check/fallback-ordering logic inside a streaming-specific copy. Reason: step 1 built it generic specifically so this step wouldn't need to.
- Don't leave any existing `tests/test_streaming.py` test calling the old `stream_chat_completion(adapter, request, on_complete)` signature. Reason: it no longer exists; every direct caller must be updated, not just the one this doc calls out by name.
- Do not break existing tests.
