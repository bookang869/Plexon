"""Tests for gateway/ratelimit/budget.py + the budget-check branch of POST
/v1/chat/completions (ADR-004, ADR-018). Runs against the real app
(in-process), real Redis/Postgres, and the real mock-openai container --
`docker compose -f deploy/docker-compose.yml up -d redis postgres mock-openai
mock-anthropic`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio

from gateway.config.loader import ModelPricing, PricingConfig, start_config_watcher
from gateway.main import app
from gateway.ratelimit.budget import _daily_key, compute_cost
from gateway.schemas import Usage

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


async def _insert_team(db_pool, *, daily_budget_usd, monthly_budget_usd) -> dict:
    team_id = f"team-test-{uuid.uuid4().hex[:8]}"
    api_key = f"test-key-{uuid.uuid4().hex}"
    await db_pool.execute(
        """
        INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit,
                            daily_budget_usd, monthly_budget_usd, config)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        team_id,
        "Budget Test Team",
        ["gpt-4o-mini"],
        1000,
        100_000,
        daily_budget_usd,
        monthly_budget_usd,
        "{}",
    )
    await db_pool.execute(
        "INSERT INTO team_api_keys (token, team_id) VALUES ($1, $2)", api_key, team_id
    )
    return {"team_id": team_id, "api_key": api_key}


async def _delete_team(db_pool, team_id: str) -> None:
    await db_pool.execute("DELETE FROM spend_ledger WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM team_api_keys WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM teams WHERE id = $1", team_id)


@pytest_asyncio.fixture
async def budget_team(db_pool):
    """$10/day, $200/month, starting at zero spend -- well under budget."""
    team = await _insert_team(db_pool, daily_budget_usd="10.00", monthly_budget_usd="200.00")
    yield team
    await _delete_team(db_pool, team["team_id"])


@pytest_asyncio.fixture
async def unlimited_budget_team(db_pool):
    team = await _insert_team(db_pool, daily_budget_usd=None, monthly_budget_usd=None)
    yield team
    await _delete_team(db_pool, team["team_id"])


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _chat_payload() -> dict:
    return {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi there"}]}


# --- successful request under budget -----------------------------------------


@pytest.mark.asyncio
async def test_request_under_budget_succeeds_and_writes_ledger_row(client, db_pool, budget_team):
    resp = await client.post(
        "/v1/chat/completions", json=_chat_payload(), headers=_auth_headers(budget_team["api_key"])
    )
    assert resp.status_code == 200
    usage = resp.json()["usage"]

    pricing = ModelPricing(input_per_1k=0.00015, output_per_1k=0.0006)
    expected_cost = (Decimal(usage["prompt_tokens"]) / Decimal(1000)) * Decimal(
        str(pricing.input_per_1k)
    ) + (Decimal(usage["completion_tokens"]) / Decimal(1000)) * Decimal(str(pricing.output_per_1k))

    row = await db_pool.fetchrow(
        "SELECT * FROM spend_ledger WHERE team_id = $1 ORDER BY id DESC LIMIT 1",
        budget_team["team_id"],
    )
    assert row is not None
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-4o-mini"
    assert row["input_tokens"] == usage["prompt_tokens"]
    assert row["output_tokens"] == usage["completion_tokens"]
    assert Decimal(row["cost_usd"]) == expected_cost


# --- exhausted budget blocks with 402 ----------------------------------------


@pytest.mark.asyncio
async def test_team_over_daily_budget_returns_402_and_writes_no_ledger_row(
    client, db_pool, redis_client, budget_team
):
    await redis_client.set(_daily_key(budget_team["team_id"]), "10.00")

    before_count = await db_pool.fetchval(
        "SELECT count(*) FROM spend_ledger WHERE team_id = $1", budget_team["team_id"]
    )

    resp = await client.post(
        "/v1/chat/completions", json=_chat_payload(), headers=_auth_headers(budget_team["api_key"])
    )
    assert resp.status_code == 402

    after_count = await db_pool.fetchval(
        "SELECT count(*) FROM spend_ledger WHERE team_id = $1", budget_team["team_id"]
    )
    assert after_count == before_count


# --- warning header at >=80% but <100% ---------------------------------------


@pytest.mark.asyncio
async def test_team_at_80_percent_utilization_gets_warning_header(
    client, redis_client, budget_team
):
    await redis_client.set(_daily_key(budget_team["team_id"]), "8.50")

    resp = await client.post(
        "/v1/chat/completions", json=_chat_payload(), headers=_auth_headers(budget_team["api_key"])
    )
    assert resp.status_code == 200
    assert resp.headers["X-Budget-Warning"] == "true"


# --- unlimited budget never blocks or warns -----------------------------------


@pytest.mark.asyncio
async def test_team_without_budget_configured_never_blocked_or_warned(
    client, redis_client, unlimited_budget_team
):
    # Set an absurdly large spend counter directly -- with no configured
    # budget field, check_budget must never even look at it.
    await redis_client.set(_daily_key(unlimited_budget_team["team_id"]), "999999999.00")

    resp = await client.post(
        "/v1/chat/completions",
        json=_chat_payload(),
        headers=_auth_headers(unlimited_budget_team["api_key"]),
    )
    assert resp.status_code == 200
    assert "X-Budget-Warning" not in resp.headers


# --- compute_cost error on missing pricing entry ------------------------------


def test_compute_cost_raises_for_unpriced_model():
    pricing = PricingConfig(
        openai={"gpt-4o-mini": ModelPricing(input_per_1k=0.00015, output_per_1k=0.0006)},
        anthropic={},
        ollama={},
    )
    usage = Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20)

    with pytest.raises(ValueError):
        compute_cost(usage, "openai", "not-a-real-model", pricing)

    with pytest.raises(ValueError):
        compute_cost(usage, "anthropic", "claude-sonnet", pricing)
