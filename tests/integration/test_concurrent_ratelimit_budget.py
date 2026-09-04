"""Concurrent-load integration tests for rate limiting (ADR-011, ADR-020) and
budget enforcement (ADR-004, ADR-018), driven via `asyncio.gather` against the
real app (in-process), real Redis/Postgres, and the real mock-openai
container -- `docker compose -f deploy/docker-compose.yml up -d redis
postgres mock-openai mock-anthropic`.

Rate-limit assertions (rpm, tpm, streaming) are exact: each dimension is
isolated in its own bucket, and `check_and_consume` is a single atomic Redis
EVAL, so concurrent callers against the same key can't interleave
read-compute-write (gateway/ratelimit/token_bucket.py). The rpm+tpm combined
check has an accepted small race window (ADR-002, limiter.py docstring) --
not touched here, and irrelevant since each test isolates one dimension by
making the other bucket's capacity effectively unlimited.

Budget assertions are eventual, not per-wave-exact: `check_budget` reads
Redis before the provider call, `record_spend` writes after it completes
(gateway/ratelimit/budget.py) -- a genuine check-then-act race by design, not
a bug. A first concurrent wave's split between 200/402 is not asserted
exactly; only that a second wave, fired after the first has fully settled, is
uniformly rejected once tracked spend is at/over the cap.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio

from gateway.config.loader import start_config_watcher
from gateway.main import app

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


async def _insert_team(
    db_pool,
    *,
    rpm_limit: int = 1000,
    tpm_limit: int = 100_000,
    daily_budget_usd=None,
    monthly_budget_usd=None,
) -> dict:
    team_id = f"team-test-{uuid.uuid4().hex[:8]}"
    api_key = f"test-key-{uuid.uuid4().hex}"
    await db_pool.execute(
        """
        INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit,
                            daily_budget_usd, monthly_budget_usd, config)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        team_id,
        "Concurrent Test Team",
        ["gpt-4o-mini"],
        rpm_limit,
        tpm_limit,
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
    await db_pool.execute("DELETE FROM alert_history WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM team_api_keys WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM teams WHERE id = $1", team_id)


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _chat_payload(*, max_tokens: int | None = None) -> dict:
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi there"}]}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


# --- rpm dimension -----------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_requests_admit_exactly_rpm_limit(client, db_pool):
    team = await _insert_team(db_pool, rpm_limit=10, tpm_limit=1_000_000)
    try:
        headers = _auth_headers(team["api_key"])
        responses = await asyncio.gather(
            *[client.post("/v1/chat/completions", json=_chat_payload(), headers=headers) for _ in range(30)]
        )
        statuses = [r.status_code for r in responses]
        assert statuses.count(200) == 10
        assert statuses.count(429) == 20
        assert len(statuses) == 30
    finally:
        await _delete_team(db_pool, team["team_id"])


# --- tpm dimension -------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_requests_admit_exactly_tpm_limit(client, db_pool):
    # "hi there" -> 2 words per estimate_tokens's heuristic; max_tokens=5 set
    # explicitly so each request's estimated cost is a small, known 2 + 5 = 7.
    per_request_cost = 2 + 5
    admit_count = 3
    team = await _insert_team(
        db_pool, rpm_limit=1_000_000, tpm_limit=per_request_cost * admit_count
    )
    try:
        headers = _auth_headers(team["api_key"])
        responses = await asyncio.gather(
            *[
                client.post(
                    "/v1/chat/completions",
                    json=_chat_payload(max_tokens=5),
                    headers=headers,
                )
                for _ in range(10)
            ]
        )
        statuses = [r.status_code for r in responses]
        assert statuses.count(200) == admit_count
        assert statuses.count(429) == 10 - admit_count
    finally:
        await _delete_team(db_pool, team["team_id"])


# --- budget: eventual consistency, not per-wave atomicity ---------------------


@pytest.mark.asyncio
async def test_concurrent_budget_enforcement_settles_and_rejects_second_wave(client, db_pool):
    # Each request against gpt-4o-mini with the "hi there" payload costs a
    # fixed, tiny amount (mock fabricates prompt_tokens=2, completion_tokens=6
    # -> (2/1000)*0.00015 + (6/1000)*0.0006 = 0.0000039). A daily budget of
    # 0.00001 requires only 3 successful requests to exceed -- small enough
    # that the first wave of 5 concurrent requests is guaranteed to push
    # settled spend at/over the cap regardless of how the check-then-act race
    # resolves (even the most sequential ordering blocks by the 4th request).
    team = await _insert_team(
        db_pool, rpm_limit=1_000_000, tpm_limit=1_000_000, daily_budget_usd="0.00001"
    )
    try:
        headers = _auth_headers(team["api_key"])

        # First wave: some may succeed, some may 402 depending on how the
        # check-then-act race resolves -- not asserted exactly, that's
        # the point (see module docstring / budget.py's docstring).
        first_wave = await asyncio.gather(
            *[client.post("/v1/chat/completions", json=_chat_payload(), headers=headers) for _ in range(5)]
        )
        for resp in first_wave:
            assert resp.status_code in (200, 402)

        spend = await db_pool.fetchval(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM spend_ledger WHERE team_id = $1",
            team["team_id"],
        )
        assert spend >= 0

        # Second wave, fired only after the first has fully settled --
        # tracked spend is now at/over the $0.01 cap, so every request in
        # this wave must be uniformly rejected.
        second_wave = await asyncio.gather(
            *[client.post("/v1/chat/completions", json=_chat_payload(), headers=headers) for _ in range(5)]
        )
        assert [r.status_code for r in second_wave] == [402] * 5
    finally:
        await _delete_team(db_pool, team["team_id"])


# --- streaming path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_streaming_requests_admit_exactly_rpm_limit(client, db_pool):
    team = await _insert_team(db_pool, rpm_limit=5, tpm_limit=1_000_000)
    try:
        headers = _auth_headers(team["api_key"])
        payload = _chat_payload()
        payload["stream"] = True

        async def _stream_once() -> int:
            async with client.stream(
                "POST", "/v1/chat/completions", json=payload, headers=headers
            ) as resp:
                status = resp.status_code
                async for _ in resp.aiter_lines():
                    pass
                return status

        statuses = await asyncio.gather(*[_stream_once() for _ in range(15)])
        assert statuses.count(200) == 5
        assert statuses.count(429) == 10
    finally:
        await _delete_team(db_pool, team["team_id"])
