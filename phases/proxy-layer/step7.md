# Step 7: streaming

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ADR.md` — read ADR-009 (streaming — per-provider real-time translation with tee-logging) very closely; it is the entire spec for this step
- `/docs/PRD.md` — the "Open Risks" section flags streaming translation as "the most implementation-heavy piece of Phase 1 — budget extra time here"
- `/docs/TRD.md` — §3's closing paragraph on streaming, §6.1 (the `stream: true` request field)
- `phases/proxy-layer/step1.md`'s actual output: `gateway/schemas.py` — `ChatCompletionChunk` (the SSE payload shape)
- `phases/proxy-layer/step2.md`'s actual output: the mocks' streaming behavior — read the actual SSE formats `mock-openai` and `mock-anthropic` produce, since that's what step 3's adapters consume
- `phases/proxy-layer/step3.md`'s actual output: `gateway/providers/` — each adapter's `chat_completion_stream(request) -> AsyncIterator[ChatCompletionChunk]` method
- `phases/proxy-layer/step6.md`'s actual output: `gateway/main.py`/`gateway/routes.py` — the existing non-streaming `POST /v1/chat/completions` handler this step extends (don't duplicate the auth/allowed-model/content-filter/enrichment logic — reuse it)

## Task

Extend `POST /v1/chat/completions` to handle `request.stream == True`, per ADR-009: translate each provider's native stream chunks into OpenAI-style SSE chunks **as they arrive** (not buffered-then-sent), while simultaneously teeing every chunk into an in-memory buffer so the full response can be reconstructed once the stream ends (for future logging/observability — this phase doesn't write that log yet, but the buffer/assembly mechanism must exist and be exercised by a test, since it's the hard part later phases build on).

### Handler shape

Reuse steps 1-5 of the existing non-streaming handler (auth, allowed-model check, content filter, enrichment) unchanged — branch on `request.stream` only at the point of calling the provider:

```python
async def stream_chat_completion(adapter: ProviderAdapter, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
    """Yields SSE-formatted bytes (`data: {...}\n\n`), ending with `data: [DONE]\n\n`.
    Tees every chunk into an assembled-response buffer as it's yielded."""
```

Return this from the route via FastAPI's `StreamingResponse` with `media_type="text/event-stream"`.

### Tee / assembly

As each `ChatCompletionChunk` arrives from `adapter.chat_completion_stream(...)`, in addition to immediately serializing and yielding it:
- Accumulate `delta.content` fragments in order to reconstruct the full assistant message text.
- Track the last `finish_reason` seen.
- Once the stream is exhausted, you have the shape of a complete `ChatCompletionResponse` **except `usage`**, since token counts aren't known until the provider's stream signals completion (or, for providers whose streaming API doesn't include usage on the final chunk, this is a genuine limitation — handle it gracefully: if usage is unavailable, use a best-effort estimate or omit it, but don't crash the request over it. Note in a docstring which case applies to `mock-openai`/`mock-anthropic`/Ollama based on what step 2/3 actually built).
- Because nothing consumes this assembled response yet (spend-ledger logging is a later phase), the assembly function's result just needs to be correct and available to a caller — expose it as a return value or callback (e.g. `on_complete: Callable[[ChatCompletionResponse], None] | None = None` parameter on `stream_chat_completion`) rather than committing to a specific consumer now, since you don't yet know exactly how the `ratelimit-budget`/`observability` phases will want to receive it.

### Error handling mid-stream

If the provider adapter raises partway through iteration (connection drop, provider error), the client has already received some chunks — you can't retroactively return a clean HTTP error status (the response already started). Send a final SSE event communicating the failure in-band (e.g. a chunk with an `error` field, or just terminate the stream early with `data: [DONE]`) rather than letting an unhandled exception propagate into an ASGI server error mid-response. Document your choice — this is a real but not fully spec'd decision.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build postgres mock-openai mock-anthropic
uv run pytest tests/test_streaming.py -v
# tests/test_streaming.py must cover, using the seed-team fixture and real mock services:
#   - streaming request to an allowed OpenAI-backed model -> SSE stream of well-formed chunks ending in [DONE]
#   - streaming request to an allowed Anthropic-backed model -> same, proving native Anthropic stream events were correctly translated per-chunk
#   - the assembled/teed response (via the on_complete callback or return value) matches what a non-streaming call to the same input would have produced (content equivalence)
#   - a mid-stream fault (X-Mock-Fault: error, if the mock supports triggering it after the stream starts — otherwise inject via a stubbed adapter) doesn't produce an unhandled server exception
#   - non-streaming requests (from step 6) still pass — this step must not have regressed them
docker compose -f deploy/docker-compose.yml down
```

## Verification Procedure

1. Run the AC commands above, and also re-run step 6's test file (`uv run pytest tests/test_routing.py -v`) to confirm no regression.
2. Check the architecture checklist:
   - Are chunks translated and yielded in real time, not buffered and sent all at once (the whole point of ADR-009)?
   - Does the tee/assembly path produce a result equivalent to what non-streaming would have returned for the same input?
   - Is auth/allowed-model/content-filter/enrichment logic reused from step 6, not duplicated?
3. Based on the result, update `phases/proxy-layer/index.json` step 7:
   - Success → `"status": "completed"`, `"summary": "one-line summary — streaming function name, tee/assembly mechanism, error-handling choice made, marking proxy-layer phase complete"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't buffer the entire provider stream before sending anything to the client. Reason: defeats the purpose of streaming (ADR-009) and would misrepresent latency-sensitive behavior in the eventual demo.
- Don't duplicate the auth/enrichment/content-filter/allowed-model logic from step 6's handler. Reason: single source of truth — if that logic changes later, it shouldn't need to change in two places.
- Don't wire the assembled response into a spend-ledger write or OTel span. Reason: those don't exist until later phases — this step only needs to make the assembled data available, not consume it.
- Do not break existing tests, especially `tests/test_routing.py` from step 6.
