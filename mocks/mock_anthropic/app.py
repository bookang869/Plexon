"""Standalone mock of the Anthropic Messages API (ADR-006). Returns
Anthropic's real native response shape ({type: "message", content: [...],
usage: {input_tokens, output_tokens}, ...}) -- NOT pre-translated to the
OpenAI schema. That translation is the gateway's AnthropicAdapter's job
(step 3); this mock exists to be realistic, not to do the adapter's work.
"""

from __future__ import annotations

import json
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
    return f"Mock Anthropic reply to: {last_user}"


def _fabricate_usage(prompt_text: str, completion_text: str) -> dict:
    input_tokens = max(1, len(prompt_text.split()))
    output_tokens = max(1, len(completion_text.split()))
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _sse_event(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def _stream_events(
    message_id: str, model: str, reply_text: str, input_tokens: int
) -> AsyncIterator[bytes]:
    yield _sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        },
    )
    yield _sse_event(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )

    words = reply_text.split()
    for i, word in enumerate(words):
        text = word if i == len(words) - 1 else f"{word} "
        yield _sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )

    yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": max(1, len(words))},
        },
    )
    yield _sse_event("message_stop", {"type": "message_stop"})


@app.post("/v1/messages")
async def messages(
    request: Request,
    x_mock_fault: str | None = Header(default=None, alias="X-Mock-Fault"),
):
    body = await request.json()
    fault_type, model = extract_fault_and_model(body.get("model", ""), x_mock_fault)

    fault_response = await apply_fault(fault_type)
    if fault_response is not None:
        return fault_response

    messages_in = body.get("messages", [])
    reply_text = _fabricate_reply(messages_in)
    prompt_text = body.get("system", "") + " " + " ".join(
        m.get("content", "") for m in messages_in
    )
    usage = _fabricate_usage(prompt_text, reply_text)
    message_id = f"msg_{uuid.uuid4().hex[:24]}"

    if body.get("stream"):
        return StreamingResponse(
            _stream_events(message_id, model, reply_text, usage["input_tokens"]),
            media_type="text/event-stream",
        )

    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": reply_text}],
        "model": model,
        "stop_reason": "end_turn",
        "usage": usage,
    }
