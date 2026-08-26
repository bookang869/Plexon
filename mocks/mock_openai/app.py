"""Standalone mock of the OpenAI Chat Completions API (ADR-006). Response
shape matches gateway/schemas.py's ChatCompletionResponse/ChatCompletionChunk
since the real OpenAI API already speaks near-native OpenAI format -- this
mock does not import from gateway/, it just mirrors that shape by hand.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, Header, Request
from fastapi.responses import StreamingResponse

from mocks.fault_injection import apply_fault, extract_fault_and_model

app = FastAPI()


def _fabricate_reply(messages: list[dict]) -> str:
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
    )
    return f"Mock OpenAI reply to: {last_user}"


def _fabricate_usage(prompt_text: str, completion_text: str) -> dict:
    prompt_tokens = max(1, len(prompt_text.split()))
    completion_tokens = max(1, len(completion_text.split()))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


async def _stream_chunks(
    completion_id: str, created: int, model: str, reply_text: str
) -> AsyncIterator[bytes]:
    words = reply_text.split()
    for i, word in enumerate(words):
        content = word if i == len(words) - 1 else f"{word} "
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": content},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()
    final_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    x_mock_fault: str | None = Header(default=None, alias="X-Mock-Fault"),
):
    body = await request.json()
    fault_type, model = extract_fault_and_model(body.get("model", ""), x_mock_fault)

    fault_response = await apply_fault(fault_type)
    if fault_response is not None:
        return fault_response

    messages = body.get("messages", [])
    reply_text = _fabricate_reply(messages)
    prompt_text = " ".join(m.get("content", "") for m in messages)
    usage = _fabricate_usage(prompt_text, reply_text)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if body.get("stream"):
        return StreamingResponse(
            _stream_chunks(completion_id, created, model, reply_text),
            media_type="text/event-stream",
        )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply_text},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
