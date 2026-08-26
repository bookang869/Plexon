"""Tests for gateway/routes.py + gateway/providers/registry.py -- the direct
auth -> enrich -> provider -> response path (TRD §3 steps 1,2,5,6,7,8,10).
Runs against the real app (in-process) and the real mock-openai/mock-anthropic
containers (`docker compose up -d postgres mock-openai mock-anthropic`).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio

from gateway.config.loader import start_config_watcher
from gateway.main import app

_config_loaded = False


@pytest_asyncio.fixture
async def client(db_pool):
    global _config_loaded
    if not _config_loaded:
        start_config_watcher()
        _config_loaded = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as ac:
        yield ac


@pytest_asyncio.fixture
async def team_with_unknown_model(db_pool):
    """A team allowed to use a model no configured provider serves."""
    team_id = f"team-test-{uuid.uuid4().hex[:8]}"
    api_key = f"test-key-{uuid.uuid4().hex}"

    await db_pool.execute(
        """
        INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit,
                            daily_budget_usd, monthly_budget_usd, config)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        team_id,
        "Unknown Model Test Team",
        ["totally-fake-model"],
        60,
        10000,
        None,
        None,
        "{}",
    )
    await db_pool.execute(
        "INSERT INTO team_api_keys (token, team_id) VALUES ($1, $2)",
        api_key,
        team_id,
    )

    yield {"team_id": team_id, "api_key": api_key}

    await db_pool.execute("DELETE FROM team_api_keys WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM teams WHERE id = $1", team_id)


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


# --- POST /v1/chat/completions ---------------------------------------------


@pytest.mark.asyncio
async def test_valid_request_to_allowed_model_returns_200(client, seeded_team):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi there"}]},
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] == (
        body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )


@pytest.mark.asyncio
async def test_model_not_in_allowed_models_returns_403(client, seeded_team):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "claude-opus", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_model_no_provider_serves_returns_404(client, team_with_unknown_model):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "totally-fake-model", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth_headers(team_with_unknown_model["api_key"]),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_content_matching_blocklist_returns_400(client, seeded_team):
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "this has a forbidden-term in it"}],
        },
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_missing_auth_returns_401(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invalid_auth_returns_401(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth_headers("nonexistent-key"),
    )
    assert resp.status_code == 401


# --- GET /v1/models ----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_models_returns_teams_allowed_models(client, seeded_team):
    resp = await client.get("/v1/models", headers=_auth_headers(seeded_team["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert [m["id"] for m in body["data"]] == ["gpt-4o-mini", "claude-sonnet"]


@pytest.mark.asyncio
async def test_list_models_requires_auth(client):
    resp = await client.get("/v1/models")
    assert resp.status_code == 401
