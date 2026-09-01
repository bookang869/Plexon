"""Real-time SSE translation with tee/assembly (ADR-009). Provider adapters
(step 3) already translate each provider's native stream into canonical
`ChatCompletionChunk`s; this module's job is only to serialize those chunks
to SSE bytes *as they arrive* and, simultaneously, accumulate them into a
complete `ChatCompletionResponse` for whatever later phase wants to log/
observe it (spend ledger, OTel spans -- not this step, see gateway/routes.py).

Stream *establishment* (calling `adapter.chat_completion_stream` and pulling
the first chunk) is the caller's job as of the resilience phase's step 2 --
retry/fallback across providers happens before this function is ever
invoked (see `gateway/resilience/orchestrator.py`'s `resolve_streaming_start`).
By the time `stream_chat_completion` runs, a candidate has already produced
at least one chunk successfully.

Usage limitation: no adapter's `chat_completion_stream` carries token usage
today -- `ChatCompletionChunk` (step 1) has no `usage` field, mock-openai's
SSE never emits one, and AnthropicAdapter's stream translation drops the
input/output counts present on Anthropic's native `message_start`/
`message_delta` events rather than plumbing them through. Until a later
phase gives streaming chunks somewhere to carry real usage, the assembled
response's `usage` is a word-count-based best-effort estimate -- the same
fabrication the mocks already use for their non-streaming responses.
"""

from __future__ import annotations

import inspect
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from gateway.providers.errors import ProviderError
from gateway.schemas import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)


def _estimate_usage(request: ChatCompletionRequest, completion_text: str) -> Usage:
    prompt_text = " ".join(m.content for m in request.messages)
    prompt_tokens = max(1, len(prompt_text.split()))
    completion_tokens = max(1, len(completion_text.split()))
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


async def stream_chat_completion(
    chunks: AsyncIterator[ChatCompletionChunk],
    first_chunk: ChatCompletionChunk,
    request: ChatCompletionRequest,
    on_complete: Callable[[ChatCompletionResponse], Awaitable[None] | None] | None = None,
) -> AsyncIterator[bytes]:
    """Yields SSE-formatted bytes (`data: {...}\\n\\n`), ending with
    `data: [DONE]\\n\\n`. `chunks` is an already-in-flight async iterator
    (from some adapter's `chat_completion_stream`) whose first item has
    already been pulled out as `first_chunk` by the caller (as part of the
    retry/fallback-eligible stream-establishment window -- see module
    docstring). Tees every chunk (`first_chunk`, then the rest of `chunks`)
    into an assembled-response buffer as it's yielded; once the stream ends
    successfully, calls `on_complete` (if given) with the assembled
    `ChatCompletionResponse`. `on_complete` may be sync or async -- an
    awaitable return value is awaited, a bare `None` is not.

    Mid-stream faults: by the time the first chunk is yielded, the HTTP
    response has already started (status 200, headers sent) -- there's no
    clean status code left to return if `chunks` then raises. Instead of
    letting the exception propagate into an ASGI server error, this catches
    `ProviderError` from `chunks`' iterator, emits one final chunk carrying
    an `error` field, then terminates with `[DONE]`. `on_complete` is not
    called in that case, since no complete response exists.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = request.model
    content_parts: list[str] = []
    finish_reason: str | None = None

    def _consume(chunk: ChatCompletionChunk) -> bytes:
        nonlocal completion_id, created, model, finish_reason
        completion_id, created, model = chunk.id, chunk.created, chunk.model
        for choice in chunk.choices:
            if choice.delta.content:
                content_parts.append(choice.delta.content)
            if choice.finish_reason:
                finish_reason = choice.finish_reason
        return f"data: {chunk.model_dump_json()}\n\n".encode()

    try:
        yield _consume(first_chunk)
        async for chunk in chunks:
            yield _consume(chunk)
    except ProviderError as exc:
        error_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "error": {"message": str(exc), "type": "provider_error"},
        }
        yield f"data: {json.dumps(error_chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"
        return

    yield b"data: [DONE]\n\n"

    if on_complete is not None:
        completion_text = "".join(content_parts)
        assembled = ChatCompletionResponse(
            id=completion_id,
            created=created,
            model=model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=completion_text),
                    finish_reason=finish_reason,
                )
            ],
            usage=_estimate_usage(request, completion_text),
        )
        result = on_complete(assembled)
        if inspect.isawaitable(result):
            await result
