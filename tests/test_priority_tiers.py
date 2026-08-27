"""Tests for gateway/ratelimit/limiter.py + the rate-limit-check branch of
POST /v1/chat/completions (ADR-011, ADR-020). Runs against the real app
(in-process), real Redis/Postgres, and the real mock-openai container --
`docker compose -f deploy/docker-compose.yml up -d redis postgres mock-openai
mock-anthropic`.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio

from gateway.config.loader import start_config_watcher
from gateway.main import app
from gateway.ratelimit.token_bucket import check_and_consume

_config_loaded = False

# Matches the tpm_limit the shared `seeded_team` fixture (tests/conftest.py)
# inserts -- large enough to never be the bottleneck unless a test deliberately
# drives estimated/actual usage close to it.
_SEEDED_TEAM_TPM_LIMIT = 10000.0


@pytest_asyncio.fixture
async def client(db_pool, redis_client):
    global _config_loaded
    if not _config_loaded:
        start_config_watcher()
        _config_loaded = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as ac:
        yield ac


async def _insert_team(db_pool, *, rpm_limit: int, tpm_limit: int) -> dict:
    team_id = f"team-test-{uuid.uuid4().hex[:8]}"
    api_key = f"test-key-{uuid.uuid4().hex}"
    await db_pool.execute(
        """
        INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit,
                            daily_budget_usd, monthly_budget_usd, config)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        team_id,
        "Priority Tier Test Team",
        ["gpt-4o-mini"],
        rpm_limit,
        tpm_limit,
        None,
        None,
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
async def low_rpm_team(db_pool):
    """rpm ceiling of 2, tpm effectively unlimited -- isolates rpm denial."""
    team = await _insert_team(db_pool, rpm_limit=2, tpm_limit=100_000)
    yield team
    await _delete_team(db_pool, team["team_id"])


@pytest_asyncio.fixture
async def low_tpm_team(db_pool):
    """rpm ceiling of 2, tpm ceiling of 50 -- small enough that a large
    max_tokens request trips the tpm check without exhausting rpm.
    """
    team = await _insert_team(db_pool, rpm_limit=2, tpm_limit=50)
    yield team
    await _delete_team(db_pool, team["team_id"])


@pytest_asyncio.fixture
async def tier_team(db_pool):
    """rpm ceiling of 10, tpm effectively unlimited -- realtime's 100% ceiling
    (capacity 10) vs batch's 60% ceiling (capacity 6) per config.yaml.
    """
    team = await _insert_team(db_pool, rpm_limit=10, tpm_limit=100_000)
    yield team
    await _delete_team(db_pool, team["team_id"])


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _chat_payload(*, max_tokens: int | None = None) -> dict:
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi there"}]}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


async def _peek_tpm_remaining(redis_client, team_id: str, tier: str, capacity: float) -> float:
    """Reads current bucket state without consuming (cost=0)."""
    result = await check_and_consume(
        redis_client,
        f"ratelimit:{team_id}:{tier}:tpm",
        capacity=capacity,
        refill_per_second=capacity / 60,
        cost=0,
    )
    return result.remaining


# --- basic admission ---------------------------------------------------------


@pytest.mark.asyncio
async def test_request_within_rpm_limit_succeeds(client, seeded_team):
    resp = await client.post(
        "/v1/chat/completions", json=_chat_payload(), headers=_auth_headers(seeded_team["api_key"])
    )
    assert resp.status_code == 200


# --- rpm ceiling ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_exceeding_rpm_limit_returns_429_with_retry_after(client, low_rpm_team):
    headers = _auth_headers(low_rpm_team["api_key"])
    for _ in range(2):
        resp = await client.post("/v1/chat/completions", json=_chat_payload(), headers=headers)
        assert resp.status_code == 200

    resp = await client.post("/v1/chat/completions", json=_chat_payload(), headers=headers)
    assert resp.status_code == 429
    assert float(resp.headers["Retry-After"]) > 0


# --- tiered ceilings -------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_tier_ceiling_lower_than_realtime_for_same_team(client, tier_team):
    headers = _auth_headers(tier_team["api_key"])

    batch_statuses = []
    for _ in range(7):
        resp = await client.post(
            "/v1/chat/completions", json=_chat_payload(), headers={**headers, "X-Priority": "batch"}
        )
        batch_statuses.append(resp.status_code)
    assert batch_statuses == [200] * 6 + [429]

    realtime_statuses = []
    for _ in range(7):
        resp = await client.post(
            "/v1/chat/completions",
            json=_chat_payload(),
            headers={**headers, "X-Priority": "realtime"},
        )
        realtime_statuses.append(resp.status_code)
    assert realtime_statuses == [200] * 7


# --- unknown tier ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_priority_header_returns_400(client, seeded_team):
    resp = await client.post(
        "/v1/chat/completions",
        json=_chat_payload(),
        headers={**_auth_headers(seeded_team["api_key"]), "X-Priority": "urgent"},
    )
    assert resp.status_code == 400


# --- rollback on partial (tpm) denial -----------------------------------------------


@pytest.mark.asyncio
async def test_tpm_denial_does_not_permanently_consume_rpm_capacity(client, low_tpm_team):
    headers = _auth_headers(low_tpm_team["api_key"])

    # Estimated tokens (prompt + max_tokens) vastly exceed tpm capacity (50)
    # -- denied on the tpm check, after rpm was provisionally consumed.
    resp = await client.post(
        "/v1/chat/completions", json=_chat_payload(max_tokens=1_000_000), headers=headers
    )
    assert resp.status_code == 429

    # rpm ceiling is 2. If the earlier tpm denial had left rpm permanently
    # short by one (no rollback), only one of these two would succeed.
    for _ in range(2):
        resp = await client.post(
            "/v1/chat/completions", json=_chat_payload(max_tokens=5), headers=headers
        )
        assert resp.status_code == 200

    resp = await client.post(
        "/v1/chat/completions", json=_chat_payload(max_tokens=5), headers=headers
    )
    assert resp.status_code == 429


# --- tpm reconciliation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_streaming_reconciles_tpm_bucket_after_lower_actual_usage(
    client, redis_client, seeded_team
):
    estimated = 2 + 1000  # "hi there" -> 2 words; matches estimate_tokens's heuristic
    resp = await client.post(
        "/v1/chat/completions",
        json=_chat_payload(max_tokens=1000),
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 200

    no_reconcile_baseline = _SEEDED_TEAM_TPM_LIMIT - estimated
    remaining = await _peek_tpm_remaining(
        redis_client, seeded_team["team_id"], "realtime", capacity=_SEEDED_TEAM_TPM_LIMIT
    )
    assert remaining > no_reconcile_baseline


@pytest.mark.asyncio
async def test_streaming_reconciles_tpm_bucket_after_lower_actual_usage(
    client, redis_client, seeded_team
):
    estimated = 2 + 1000  # "hi there" -> 2 words
    payload = _chat_payload(max_tokens=1000)
    payload["stream"] = True

    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json=payload,
        headers=_auth_headers(seeded_team["api_key"]),
    ) as resp:
        assert resp.status_code == 200
        async for _ in resp.aiter_lines():
            pass

    no_reconcile_baseline = _SEEDED_TEAM_TPM_LIMIT - estimated
    remaining = await _peek_tpm_remaining(
        redis_client, seeded_team["team_id"], "realtime", capacity=_SEEDED_TEAM_TPM_LIMIT
    )
    assert remaining > no_reconcile_baseline
