"""OllamaAdapter talks to a real Ollama server's native /api/chat endpoint,
translating prompt_eval_count/eval_count into the canonical usage object
(ADR-019). Per ADR-017, Ollama isn't installed on the dev machine at plan
time: this adapter's automated tests use a stubbed HTTP transport, never a
live server. Live-call verification against a real running Ollama server is
a manual step, deferred until Ollama is installed locally.
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

_CHAT_PATH = "/api/chat"
_TAGS_PATH = "/api/tags"


def _to_ollama_request(request: ChatCompletionRequest, stream: bool) -> dict:
    return {
        "model": request.model,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "stream": stream,
    }


def _from_ollama_response(data: dict) -> ChatCompletionResponse:
    prompt_tokens = data.get("prompt_eval_count", 0)
    completion_tokens = data.get("eval_count", 0)
    return ChatCompletionResponse(
        id=f"ollama-{uuid.uuid4().hex[:24]}",
        created=int(time.time()),
        model=data["model"],
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(
                    role="assistant", content=data.get("message", {}).get("content", "")
                ),
                finish_reason="stop" if data.get("done") else None,
            )
        ],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


class OllamaAdapter:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient()

    async def chat_completion(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        payload = _to_ollama_request(request, stream=False)
        try:
            resp = await self._client.post(f"{self._base_url}{_CHAT_PATH}", json=payload)
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, "ollama") from exc
        raise_for_provider_status(resp, "ollama")
        return _from_ollama_response(resp.json())

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        payload = _to_ollama_request(request, stream=True)
        completion_id = f"ollama-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        try:
            async with self._client.stream(
                "POST", f"{self._base_url}{_CHAT_PATH}", json=payload
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise_for_provider_status(resp, "ollama")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    content = data.get("message", {}).get("content", "")
                    finish_reason = "stop" if data.get("done") else None
                    yield ChatCompletionChunk(
                        id=completion_id,
                        created=created,
                        model=data.get("model", request.model),
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0,
                                delta=ChatCompletionChunkDelta(content=content),
                                finish_reason=finish_reason,
                            )
                        ],
                    )
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, "ollama") from exc

    async def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            resp = await self._client.get(f"{self._base_url}{_TAGS_PATH}", timeout=5.0)
        except httpx.HTTPError as exc:
            return HealthStatus(provider="ollama", healthy=False, error=str(exc))
        latency_ms = (time.monotonic() - start) * 1000
        return HealthStatus(provider="ollama", healthy=resp.status_code < 500, latency_ms=latency_ms)
