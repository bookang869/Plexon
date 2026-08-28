"""Tests for gateway/streaming.py + the streaming branch of POST
/v1/chat/completions (ADR-009). Route-level tests run against the real app
(in-process) and the real mock-openai/mock-anthropic containers, same as
tests/test_routing.py. Direct stream_chat_completion() tests exercise the
tee/assembly mechanism and mid-stream error handling without going through
the HTTP layer.
"""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from gateway.config.loader import start_config_watcher
from gateway.main import app
from gateway.providers.anthropic_adapter import AnthropicAdapter
from gateway.providers.errors import RetryableProviderError
from gateway.providers.openai_adapter import OpenAIAdapter
from gateway.schemas import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
)
from gateway.streaming import stream_chat_completion

OPENAI_BASE_URL = "http://localhost:8081"
ANTHROPIC_BASE_URL = "http://localhost:8082"

_config_loaded = False


@pytest_asyncio.fixture
async def client(db_pool, redis_client):
    global _config_loaded
    if not _config_loaded:
        start_config_watcher()
        _config_loaded = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as ac:
        yield ac


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


async def _read_sse(resp: httpx.Response) -> tuple[list[dict], str]:
    """Returns (parsed non-DONE data payloads, raw terminal line)."""
    lines = [line async for line in resp.aiter_lines() if line]
    data_lines = [line[len("data: ") :] for line in lines if line.startswith("data: ")]
    assert data_lines[-1] == "[DONE]"
    return [json.loads(d) for d in data_lines[:-1]], data_lines[-1]


def _assemble_content(chunks: list[dict]) -> str:
    return "".join(
        choice["delta"].get("content") or "" for chunk in chunks for choice in chunk["choices"]
    )


# --- route-level: streaming over HTTP --------------------------------------


@pytest.mark.asyncio
async def test_streaming_openai_model_returns_well_formed_sse_ending_in_done(client, seeded_team):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "stream please"}],
            "stream": True,
        },
        headers=_auth_headers(seeded_team["api_key"]),
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        chunks, _ = await _read_sse(resp)

    assert chunks
    for chunk in chunks:
        assert chunk["object"] == "chat.completion.chunk"
    assert _assemble_content(chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_streaming_anthropic_model_translates_native_events_per_chunk(client, seeded_team):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "stream please"}],
            "stream": True,
        },
        headers=_auth_headers(seeded_team["api_key"]),
    ) as resp:
        assert resp.status_code == 200
        chunks, _ = await _read_sse(resp)

    assert chunks
    for chunk in chunks:
        assert chunk["object"] == "chat.completion.chunk"
    content = _assemble_content(chunks)
    assert "Mock Anthropic reply to: stream please" in content
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_non_streaming_route_still_works(client, seeded_team):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi there"}]},
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"]


# --- direct stream_chat_completion(): tee/assembly matches non-streaming ---


@pytest.mark.asyncio
async def test_assembled_response_matches_non_streaming_openai_call():
    adapter = OpenAIAdapter(OPENAI_BASE_URL, client=httpx.AsyncClient())
    prompt = [ChatMessage(role="user", content="tell me about the tee buffer")]

    non_stream_response = await adapter.chat_completion(
        ChatCompletionRequest(model="gpt-4o-mini", messages=prompt)
    )

    assembled: list[ChatCompletionResponse] = []
    stream_request = ChatCompletionRequest(model="gpt-4o-mini", messages=prompt, stream=True)
    async for _ in stream_chat_completion(adapter, stream_request, on_complete=assembled.append):
        pass

    assert len(assembled) == 1
    assert assembled[0].choices[0].message.content == non_stream_response.choices[0].message.content
    assert assembled[0].choices[0].finish_reason == non_stream_response.choices[0].finish_reason


@pytest.mark.asyncio
async def test_assembled_response_matches_non_streaming_anthropic_call():
    adapter = AnthropicAdapter(ANTHROPIC_BASE_URL, client=httpx.AsyncClient())
    prompt = [ChatMessage(role="user", content="tell me about the tee buffer")]

    non_stream_response = await adapter.chat_completion(
        ChatCompletionRequest(model="claude-sonnet", messages=prompt)
    )

    assembled: list[ChatCompletionResponse] = []
    stream_request = ChatCompletionRequest(model="claude-sonnet", messages=prompt, stream=True)
    async for _ in stream_chat_completion(adapter, stream_request, on_complete=assembled.append):
        pass

    assert len(assembled) == 1
    assert assembled[0].choices[0].message.content == non_stream_response.choices[0].message.content
    assert assembled[0].choices[0].finish_reason == non_stream_response.choices[0].finish_reason


# --- direct stream_chat_completion(): mid-stream fault handling ------------


class _FaultInjectingAdapter:
    """Stubbed adapter (not a real provider) that yields one good chunk then
    raises mid-iteration, simulating a dropped provider connection -- the
    mocks can only fault *before* a stream starts (ADR-025), not partway
    through, so this is the only way to exercise this path.
    """

    async def chat_completion_stream(self, request: ChatCompletionRequest):
        yield ChatCompletionChunk(
            id="stub-1",
            created=0,
            model=request.model,
            choices=[
                ChatCompletionChunkChoice(
                    index=0, delta=ChatCompletionChunkDelta(role="assistant", content="partial ")
                )
            ],
        )
        raise RetryableProviderError("stub: connection dropped mid-stream")


@pytest.mark.asyncio
async def test_mid_stream_fault_emits_error_chunk_and_done_without_raising():
    request = ChatCompletionRequest(
        model="stub-model", messages=[ChatMessage(role="user", content="hi")], stream=True
    )
    on_complete_calls: list[ChatCompletionResponse] = []

    raw_chunks = [
        chunk
        async for chunk in stream_chat_completion(
            _FaultInjectingAdapter(), request, on_complete=on_complete_calls.append
        )
    ]

    text = b"".join(raw_chunks).decode()
    data_lines = [
        line[len("data: ") :] for line in text.split("\n\n") if line.startswith("data: ")
    ]
    assert data_lines[-1] == "[DONE]"
    error_payload = json.loads(data_lines[-2])
    assert error_payload["error"]["type"] == "provider_error"
    assert not on_complete_calls
