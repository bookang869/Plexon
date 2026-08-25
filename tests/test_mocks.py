import asyncio
import json

import httpx
import pytest

from mocks.mock_anthropic.app import app as anthropic_app
from mocks.mock_openai.app import app as openai_app


def _openai_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=openai_app), base_url="http://mock-openai.test")


def _anthropic_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=anthropic_app), base_url="http://mock-anthropic.test"
    )


def _sse_data_lines(text: str) -> list[str]:
    return [line[len("data: ") :] for line in text.splitlines() if line.startswith("data: ")]


# --- mock-openai ---------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_normal_response():
    async with _openai_client() as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi there"}]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "gpt-4o-mini"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["usage"]["prompt_tokens"] > 0
    assert body["usage"]["completion_tokens"] > 0
    assert body["usage"]["total_tokens"] == (
        body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )


@pytest.mark.asyncio
async def test_openai_streaming_response():
    async with _openai_client() as client, client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "stream please"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        text = "".join([chunk async for chunk in resp.aiter_text()])
    data_lines = _sse_data_lines(text)
    assert data_lines[-1] == "[DONE]"
    first_chunk = json.loads(data_lines[0])
    assert first_chunk["object"] == "chat.completion.chunk"


@pytest.mark.parametrize("fault", ["error", "rate_limit"])
@pytest.mark.asyncio
async def test_openai_fault_via_header(fault: str):
    async with _openai_client() as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Mock-Fault": fault},
        )
    if fault == "error":
        assert resp.status_code == 500
    else:
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


@pytest.mark.parametrize("fault", ["error", "rate_limit"])
@pytest.mark.asyncio
async def test_openai_fault_via_magic_suffix(fault: str):
    async with _openai_client() as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": f"gpt-4o-mini--fault-{fault}",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    if fault == "error":
        assert resp.status_code == 500
    else:
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


@pytest.mark.asyncio
async def test_openai_timeout_fault_hangs_via_header():
    async with _openai_client() as client:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                client.post(
                    "/v1/chat/completions",
                    json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"X-Mock-Fault": "timeout"},
                ),
                timeout=0.2,
            )


@pytest.mark.asyncio
async def test_openai_timeout_fault_hangs_via_magic_suffix():
    async with _openai_client() as client:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "gpt-4o-mini--fault-timeout",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                ),
                timeout=0.2,
            )


# --- mock-anthropic -------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_normal_response():
    async with _anthropic_client() as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet",
                "max_tokens": 100,
                "system": "be nice",
                "messages": [{"role": "user", "content": "hi there"}],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"][0]["type"] == "text"
    assert body["model"] == "claude-sonnet"
    assert "input_tokens" in body["usage"]
    assert "output_tokens" in body["usage"]
    assert body["usage"]["input_tokens"] > 0
    assert body["usage"]["output_tokens"] > 0


@pytest.mark.asyncio
async def test_anthropic_streaming_response():
    async with _anthropic_client() as client, client.stream(
        "POST",
        "/v1/messages",
        json={
            "model": "claude-sonnet",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "stream please"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        text = "".join([chunk async for chunk in resp.aiter_text()])
    assert "event: message_start" in text
    assert "event: content_block_delta" in text
    assert "event: message_stop" in text
    assert "[DONE]" not in text


@pytest.mark.parametrize("fault", ["error", "rate_limit"])
@pytest.mark.asyncio
async def test_anthropic_fault_via_header(fault: str):
    async with _anthropic_client() as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"X-Mock-Fault": fault},
        )
    if fault == "error":
        assert resp.status_code == 500
    else:
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


@pytest.mark.parametrize("fault", ["error", "rate_limit"])
@pytest.mark.asyncio
async def test_anthropic_fault_via_magic_suffix(fault: str):
    async with _anthropic_client() as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": f"claude-sonnet--fault-{fault}",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    if fault == "error":
        assert resp.status_code == 500
    else:
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


@pytest.mark.asyncio
async def test_anthropic_timeout_fault_hangs_via_header():
    async with _anthropic_client() as client:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                client.post(
                    "/v1/messages",
                    json={
                        "model": "claude-sonnet",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"X-Mock-Fault": "timeout"},
                ),
                timeout=0.2,
            )


@pytest.mark.asyncio
async def test_anthropic_timeout_fault_hangs_via_magic_suffix():
    async with _anthropic_client() as client:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                client.post(
                    "/v1/messages",
                    json={
                        "model": "claude-sonnet--fault-timeout",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                ),
                timeout=0.2,
            )
