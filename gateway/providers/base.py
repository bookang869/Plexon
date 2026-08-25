"""Provider adapter protocol shared by openai_adapter/anthropic_adapter/
ollama_adapter (TRD §7). Later phases (resilience's health-check loop,
ratelimit-budget's provider selection) depend on this exact shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import BaseModel

from gateway.schemas import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse


class HealthStatus(BaseModel):
    provider: str
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None


class ProviderAdapter(Protocol):
    async def chat_completion(self, request: ChatCompletionRequest) -> ChatCompletionResponse: ...

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatCompletionChunk]: ...

    async def health_check(self) -> HealthStatus: ...
