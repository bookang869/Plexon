"""AnthropicAdapter talks to mock-anthropic (Anthropic's native Messages API
in production). Unlike OpenAIAdapter this performs real translation
(ADR-008): hoisting `system`-role messages out of the message list into the
top-level `system` string, and remapping Anthropic's content-block/usage/
stop_reason response shapes to the canonical schema.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator

import httpx

from gateway.providers.base import HealthStatus
from gateway.providers.errors import raise_for_provider_status, wrap_transport_error
from gateway.schemas import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Usage,
)

_MESSAGES_PATH = "/v1/messages"
_DEFAULT_MAX_TOKENS = 1024

_FINISH_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
}


def _to_anthropic_request(request: ChatCompletionRequest, stream: bool) -> dict:
    system_parts = [m.content for m in request.messages if m.role == "system"]
    other_messages = [
        {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
    ]
    payload: dict = {
        "model": request.model,
        "max_tokens": request.max_tokens or _DEFAULT_MAX_TOKENS,
        "messages": other_messages,
        "stream": stream,
    }
    if system_parts:
        payload["system"] = "\n".join(system_parts)
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    return payload


def _from_anthropic_response(data: dict) -> ChatCompletionResponse:
    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
    usage = data.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    finish_reason = _FINISH_REASON_MAP.get(data.get("stop_reason"), data.get("stop_reason"))
    return ChatCompletionResponse(
        id=data["id"],
        created=int(time.time()),
        model=data["model"],
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=text),
                finish_reason=finish_reason,
            )
        ],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


async def _iter_sse_events(resp: httpx.Response) -> AsyncIterator[tuple[str, dict]]:
    event_type: str | None = None
    async for line in resp.aiter_lines():
        if line.startswith("event: "):
            event_type = line[len("event: ") :]
        elif line.startswith("data: ") and event_type is not None:
            yield event_type, json.loads(line[len("data: ") :])
            event_type = None


class AnthropicAdapter:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient()

    async def chat_completion(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        payload = _to_anthropic_request(request, stream=False)
        try:
            resp = await self._client.post(f"{self._base_url}{_MESSAGES_PATH}", json=payload)
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, "anthropic") from exc
        raise_for_provider_status(resp, "anthropic")
        return _from_anthropic_response(resp.json())

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        payload = _to_anthropic_request(request, stream=True)
        message_id = f"msg_{uuid.uuid4().hex[:24]}"
        model = request.model
        created = int(time.time())
        try:
            async with self._client.stream(
                "POST", f"{self._base_url}{_MESSAGES_PATH}", json=payload
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise_for_provider_status(resp, "anthropic")
                async for event_type, data in _iter_sse_events(resp):
                    if event_type == "message_start":
                        message_id = data["message"]["id"]
                        model = data["message"].get("model", model)
                    elif event_type == "content_block_delta":
                        text = data.get("delta", {}).get("text", "")
                        yield ChatCompletionChunk(
                            id=message_id,
                            created=created,
                            model=model,
                            choices=[
                                ChatCompletionChunkChoice(
                                    index=0, delta=ChatCompletionChunkDelta(content=text)
                                )
                            ],
                        )
                    elif event_type == "message_delta":
                        stop_reason = data.get("delta", {}).get("stop_reason")
                        finish_reason = _FINISH_REASON_MAP.get(stop_reason, stop_reason)
                        yield ChatCompletionChunk(
                            id=message_id,
                            created=created,
                            model=model,
                            choices=[
                                ChatCompletionChunkChoice(
                                    index=0,
                                    delta=ChatCompletionChunkDelta(),
                                    finish_reason=finish_reason,
                                )
                            ],
                        )
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, "anthropic") from exc

    async def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            resp = await self._client.get(self._base_url, timeout=5.0)
        except httpx.HTTPError as exc:
            return HealthStatus(provider="anthropic", healthy=False, error=str(exc))
        latency_ms = (time.monotonic() - start) * 1000
        return HealthStatus(
            provider="anthropic", healthy=resp.status_code < 500, latency_ms=latency_ms
        )
