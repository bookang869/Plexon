"""OpenAIAdapter talks to mock-openai (real OpenAI's Chat Completions API in
production). Since mock-openai already speaks the canonical schema (step 2),
this adapter is close to a passthrough -- but it still validates/parses every
response into ChatCompletionResponse/ChatCompletionChunk rather than trusting
raw JSON, since real OpenAI's responses could still diverge from the mock's
contract at the edges.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from gateway.providers.base import HealthStatus
from gateway.providers.errors import raise_for_provider_status, wrap_transport_error
from gateway.schemas import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse

_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"


class OpenAIAdapter:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient()

    async def chat_completion(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        try:
            resp = await self._client.post(
                f"{self._base_url}{_CHAT_COMPLETIONS_PATH}",
                json=request.model_dump(exclude_none=True),
            )
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, "openai") from exc
        raise_for_provider_status(resp, "openai")
        return ChatCompletionResponse.model_validate(resp.json())

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]:
        payload = request.model_dump(exclude_none=True)
        payload["stream"] = True
        try:
            async with self._client.stream(
                "POST", f"{self._base_url}{_CHAT_COMPLETIONS_PATH}", json=payload
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise_for_provider_status(resp, "openai")
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[len("data: ") :]
                    if data == "[DONE]":
                        break
                    yield ChatCompletionChunk.model_validate(json.loads(data))
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, "openai") from exc

    async def health_check(self) -> HealthStatus:
        start = time.monotonic()
        try:
            resp = await self._client.get(self._base_url, timeout=5.0)
        except httpx.HTTPError as exc:
            return HealthStatus(provider="openai", healthy=False, error=str(exc))
        latency_ms = (time.monotonic() - start) * 1000
        return HealthStatus(provider="openai", healthy=resp.status_code < 500, latency_ms=latency_ms)
