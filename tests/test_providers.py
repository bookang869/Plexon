"""Tests for gateway/providers/*. OpenAIAdapter and AnthropicAdapter tests
run against the real mock-openai/mock-anthropic containers (`docker compose
up mock-openai mock-anthropic`, ports 8081/8082) per ADR-006. OllamaAdapter's
tests use a stubbed HTTP transport only -- ADR-017 defers live-call
verification against a real Ollama server to a manual step done once Ollama
is installed locally.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.providers.anthropic_adapter import AnthropicAdapter
from gateway.providers.errors import (
    NonRetryableProviderError,
    RetryableProviderError,
    raise_for_provider_status,
)
from gateway.providers.ollama_adapter import OllamaAdapter
from gateway.providers.openai_adapter import OpenAIAdapter
from gateway.schemas import ChatCompletionRequest, ChatMessage

OPENAI_BASE_URL = "http://localhost:8081"
ANTHROPIC_BASE_URL = "http://localhost:8082"


class _RecordingTransport(httpx.AsyncHTTPTransport):
    """Wraps the real HTTP transport, recording every outbound request so
    tests can assert on the exact JSON body sent to the mock provider.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await super().handle_async_request(request)


def _openai_adapter(timeout: float = 5.0) -> OpenAIAdapter:
    return OpenAIAdapter(OPENAI_BASE_URL, client=httpx.AsyncClient(timeout=timeout))


def _anthropic_adapter(timeout: float = 5.0) -> AnthropicAdapter:
    return AnthropicAdapter(ANTHROPIC_BASE_URL, client=httpx.AsyncClient(timeout=timeout))


# --- shared exception-typing helper --------------------------------------


def test_raise_for_provider_status_maps_codes_correctly():
    raise_for_provider_status(httpx.Response(200), "openai")  # no raise

    with pytest.raises(NonRetryableProviderError):
        raise_for_provider_status(httpx.Response(401), "openai")

    with pytest.raises(RetryableProviderError) as exc_info:
        raise_for_provider_status(httpx.Response(429, headers={"Retry-After": "2"}), "openai")
    assert exc_info.value.retry_after == 2.0

    with pytest.raises(RetryableProviderError):
        raise_for_provider_status(httpx.Response(500), "openai")


# --- OpenAIAdapter ---------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_chat_completion_non_streaming():
    adapter = _openai_adapter()
    request = ChatCompletionRequest(
        model="gpt-4o-mini", messages=[ChatMessage(role="user", content="hi there")]
    )
    response = await adapter.chat_completion(request)
    assert response.choices[0].message.role == "assistant"
    assert response.choices[0].message.content
    assert response.usage.total_tokens == (
        response.usage.prompt_tokens + response.usage.completion_tokens
    )


@pytest.mark.asyncio
async def test_openai_chat_completion_streaming():
    adapter = _openai_adapter()
    request = ChatCompletionRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="stream please")],
        stream=True,
    )
    chunks = [chunk async for chunk in adapter.chat_completion_stream(request)]
    assert chunks
    assembled = "".join(c.choices[0].delta.content or "" for c in chunks)
    assert assembled
    assert chunks[-1].choices[0].finish_reason == "stop"


@pytest.mark.parametrize("fault", ["error", "rate_limit"])
@pytest.mark.asyncio
async def test_openai_raises_retryable_on_fault(fault: str):
    adapter = _openai_adapter()
    request = ChatCompletionRequest(
        model=f"gpt-4o-mini--fault-{fault}", messages=[ChatMessage(role="user", content="hi")]
    )
    with pytest.raises(RetryableProviderError):
        await adapter.chat_completion(request)


@pytest.mark.asyncio
async def test_openai_raises_retryable_on_timeout():
    adapter = _openai_adapter(timeout=0.5)
    request = ChatCompletionRequest(
        model="gpt-4o-mini--fault-timeout", messages=[ChatMessage(role="user", content="hi")]
    )
    with pytest.raises(RetryableProviderError):
        await adapter.chat_completion(request)


# --- AnthropicAdapter ------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_chat_completion_translates_system_and_messages():
    transport = _RecordingTransport()
    adapter = AnthropicAdapter(ANTHROPIC_BASE_URL, client=httpx.AsyncClient(transport=transport))
    request = ChatCompletionRequest(
        model="claude-sonnet",
        messages=[
            ChatMessage(role="system", content="be nice"),
            ChatMessage(role="user", content="hello"),
            ChatMessage(role="assistant", content="hi, how can I help?"),
            ChatMessage(role="user", content="tell me a joke"),
        ],
    )
    response = await adapter.chat_completion(request)

    # outbound: system message hoisted out, remaining messages/roles preserved in order
    sent_body = json.loads(transport.requests[0].content)
    assert sent_body["system"] == "be nice"
    assert sent_body["max_tokens"] == 1024
    assert sent_body["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi, how can I help?"},
        {"role": "user", "content": "tell me a joke"},
    ]

    # inbound: content-block join, usage remap, finish_reason mapping
    assert response.choices[0].message.role == "assistant"
    assert "tell me a joke" in response.choices[0].message.content
    assert response.usage.prompt_tokens > 0
    assert response.usage.completion_tokens > 0
    assert response.usage.total_tokens == (
        response.usage.prompt_tokens + response.usage.completion_tokens
    )
    assert response.choices[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_anthropic_chat_completion_streaming():
    adapter = _anthropic_adapter()
    request = ChatCompletionRequest(
        model="claude-sonnet",
        messages=[ChatMessage(role="user", content="stream please")],
        stream=True,
    )
    chunks = [chunk async for chunk in adapter.chat_completion_stream(request)]
    assert chunks
    assembled = "".join(c.choices[0].delta.content or "" for c in chunks)
    assert assembled
    assert chunks[-1].choices[0].finish_reason == "stop"


@pytest.mark.parametrize("fault", ["error", "rate_limit"])
@pytest.mark.asyncio
async def test_anthropic_raises_retryable_on_fault(fault: str):
    adapter = _anthropic_adapter()
    request = ChatCompletionRequest(
        model=f"claude-sonnet--fault-{fault}", messages=[ChatMessage(role="user", content="hi")]
    )
    with pytest.raises(RetryableProviderError):
        await adapter.chat_completion(request)


@pytest.mark.asyncio
async def test_anthropic_raises_retryable_on_timeout():
    adapter = _anthropic_adapter(timeout=0.5)
    request = ChatCompletionRequest(
        model="claude-sonnet--fault-timeout", messages=[ChatMessage(role="user", content="hi")]
    )
    with pytest.raises(RetryableProviderError):
        await adapter.chat_completion(request)


# --- OllamaAdapter (stubbed transport only, ADR-017) -----------------------


@pytest.mark.asyncio
async def test_ollama_chat_completion_translates_usage():
    stub_body = {
        "model": "llama3",
        "message": {"role": "assistant", "content": "hello from llama"},
        "done": True,
        "prompt_eval_count": 12,
        "eval_count": 7,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent["messages"] == [{"role": "user", "content": "hi"}]
        assert sent["stream"] is False
        return httpx.Response(200, json=stub_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaAdapter("http://ollama.test:11434", client=client)
    request = ChatCompletionRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")])
    response = await adapter.chat_completion(request)

    assert response.choices[0].message.content == "hello from llama"
    assert response.usage.prompt_tokens == 12
    assert response.usage.completion_tokens == 7
    assert response.usage.total_tokens == 19
    assert response.choices[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_ollama_chat_completion_stream_translates_chunks():
    lines = [
        {"model": "llama3", "message": {"role": "assistant", "content": "he"}, "done": False},
        {"model": "llama3", "message": {"role": "assistant", "content": "llo"}, "done": False},
        {
            "model": "llama3",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 5,
            "eval_count": 2,
        },
    ]
    ndjson = "\n".join(json.dumps(line) for line in lines) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ndjson)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaAdapter("http://ollama.test:11434", client=client)
    request = ChatCompletionRequest(
        model="llama3", messages=[ChatMessage(role="user", content="hi")], stream=True
    )
    chunks = [chunk async for chunk in adapter.chat_completion_stream(request)]

    assembled = "".join(c.choices[0].delta.content or "" for c in chunks)
    assert assembled == "hello"
    assert chunks[-1].choices[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_ollama_chat_completion_raises_retryable_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaAdapter("http://ollama.test:11434", client=client)
    request = ChatCompletionRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")])
    with pytest.raises(RetryableProviderError):
        await adapter.chat_completion(request)
