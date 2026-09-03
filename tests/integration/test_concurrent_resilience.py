"""Concurrent-load integration tests for fallback activation, circuit-breaker
open/half-open/close cycling, and streaming integrity (PRD Core Feature 5,
TRD §10) -- driven via `asyncio.gather`/`client.stream` against the real app
(in-process), real Redis/Postgres, and the real mock-openai/mock-anthropic
containers (`docker compose -f deploy/docker-compose.yml up -d redis postgres
mock-openai mock-anthropic`).

Fault injection uses the magic model-name suffix (ADR-025), not the
`X-Mock-Fault` header, which the real adapters never forward
(tests/test_routing.py). `claude-sonnet--fault-error` (tests/fixtures/
test_config.yaml) always returns an instant HTTP 500 from mock-anthropic,
classified RetryableProviderError (gateway/providers/errors.py), driving
retry -> fallback -> circuit-breaker failures without the 30s real sleep
`--fault-timeout` would cost per attempt.

The circuit-breaker test mutates the shared `anthropic` provider's breaker
state (Redis keys keyed only by provider name, ADR-007) -- the `anthropic_
breaker` fixture resets it before and after every test in this file so this
suite doesn't leave other test files' `anthropic` traffic order-dependent.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio

from gateway.config.loader import start_config_watcher
from gateway.main import app
from gateway.resilience.circuit_breaker import (
    _failures_key,
    _opened_at_key,
    _probe_claimed_key,
    _state_key,
)
from tests.test_streaming import _assemble_content, _read_sse

_config_loaded = False

_ALLOWED_MODELS = ["claude-sonnet--fault-error", "gpt-4o-mini", "claude-sonnet"]

# test_config.yaml's shared circuit_breaker block -- not touched by this file
# (other test files depend on these exact values too).
_COOLDOWN_SECONDS = 30
_FAILURE_THRESHOLD = 5


@pytest_asyncio.fixture
async def client(db_pool, redis_client):
    global _config_loaded
    if not _config_loaded:
        start_config_watcher()
        _config_loaded = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as ac:
        yield ac


@pytest_asyncio.fixture
async def anthropic_breaker(redis_client, db_pool):
    """Resets the shared `anthropic` provider's breaker (Redis state + history
    rows) before and after each test, same technique as
    tests/test_circuit_breaker.py's `provider` fixture, but pinned to the real
    `anthropic` provider name instead of a disposable random one, since this
    file drives the breaker through real HTTP traffic against it.
    """

    async def _reset() -> None:
        await redis_client.delete(
            _state_key("anthropic"),
            _failures_key("anthropic"),
            _opened_at_key("anthropic"),
            _probe_claimed_key("anthropic"),
        )
        await db_pool.execute("DELETE FROM circuit_breaker_history WHERE provider = 'anthropic'")

    await _reset()
    yield
    await _reset()


async def _insert_team(db_pool) -> dict:
    team_id = f"team-test-{uuid.uuid4().hex[:8]}"
    api_key = f"test-key-{uuid.uuid4().hex}"
    await db_pool.execute(
        """
        INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit,
                            daily_budget_usd, monthly_budget_usd, config)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        team_id,
        "Concurrent Resilience Test Team",
        _ALLOWED_MODELS,
        1_000_000,
        1_000_000,
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
    await db_pool.execute("DELETE FROM alert_history WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM team_api_keys WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM teams WHERE id = $1", team_id)


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


# --- concurrent fallback activation ------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_fallback_activation_all_served_by_fallback(
    client, db_pool, anthropic_breaker
):
    team = await _insert_team(db_pool)
    try:
        headers = _auth_headers(team["api_key"])
        payload = {
            "model": "claude-sonnet--fault-error",
            "messages": [{"role": "user", "content": "hi there"}],
        }

        responses = await asyncio.gather(
            *[client.post("/v1/chat/completions", json=payload, headers=headers) for _ in range(10)]
        )

        assert [r.status_code for r in responses] == [200] * 10
        assert all(r.json()["model"] == "gpt-4o-mini" for r in responses)
    finally:
        await _delete_team(db_pool, team["team_id"])


# --- concurrent circuit-breaker open/half-open/close under real traffic -----


@pytest.mark.asyncio
async def test_concurrent_circuit_breaker_opens_then_recovers_through_half_open(
    client, db_pool, anthropic_breaker
):
    team = await _insert_team(db_pool)
    try:
        headers = _auth_headers(team["api_key"])
        payload = {
            "model": "claude-sonnet--fault-error",
            "messages": [{"role": "user", "content": "hi there"}],
        }

        # Comfortably exceeds failure_threshold=5 within window_seconds=60 --
        # each concurrent request contributes at most one recorded primary
        # failure (record_failure is called once per exhausted candidate,
        # after tenacity's 3 retries are spent, not once per retry attempt).
        responses = await asyncio.gather(
            *[client.post("/v1/chat/completions", json=payload, headers=headers) for _ in range(10)]
        )
        assert all(r.status_code == 200 for r in responses)
        assert all(r.json()["model"] == "gpt-4o-mini" for r in responses)

        opened_row = await db_pool.fetchrow(
            """
            SELECT * FROM circuit_breaker_history
            WHERE provider = 'anthropic' AND from_state = 'closed' AND to_state = 'open'
              AND reason = 'failure_threshold_reached'
            ORDER BY id DESC LIMIT 1
            """
        )
        assert opened_row is not None
        max_id_after_open = opened_row["id"]

        # Breaker is open, cooldown not yet elapsed -- still served by
        # fallback, but the skip path (resolve_with_resilience: `if not
        # decision.allowed: continue`) never calls record_failure, so no new
        # history row should appear for anthropic.
        resp = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["model"] == "gpt-4o-mini"

        new_rows_while_open = await db_pool.fetch(
            "SELECT * FROM circuit_breaker_history WHERE provider = 'anthropic' AND id > $1",
            max_id_after_open,
        )
        assert new_rows_while_open == []

        await asyncio.sleep(_COOLDOWN_SECONDS)

        # claude-sonnet--fault-error always fails, so the half-open probe
        # itself fails too -- still served by fallback, but anthropic's
        # breaker reopens immediately (probe_failed, no threshold wait).
        resp = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["model"] == "gpt-4o-mini"

        rows_after_cooldown = await db_pool.fetch(
            """
            SELECT from_state, to_state FROM circuit_breaker_history
            WHERE provider = 'anthropic' AND id > $1 ORDER BY id
            """,
            max_id_after_open,
        )
        transitions = [(r["from_state"], r["to_state"]) for r in rows_after_cooldown]
        assert ("open", "half_open") in transitions
        assert ("half_open", "open") in transitions
    finally:
        await _delete_team(db_pool, team["team_id"])


# --- concurrent streaming integrity -------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_streaming_no_cross_request_state_leakage(client, db_pool):
    team = await _insert_team(db_pool)
    try:
        headers = _auth_headers(team["api_key"])
        models = ["gpt-4o-mini", "claude-sonnet"]

        async def _stream_one(idx: int) -> tuple[str, str, list[dict]]:
            model = models[idx % len(models)]
            marker = f"distinctive-marker-{idx}"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": marker}],
                "stream": True,
            }
            async with client.stream(
                "POST", "/v1/chat/completions", json=payload, headers=headers
            ) as resp:
                assert resp.status_code == 200
                chunks, _ = await _read_sse(resp)
            return model, marker, chunks

        results = await asyncio.gather(*[_stream_one(i) for i in range(10)])

        for model, marker, chunks in results:
            assert chunks
            assert all(chunk["model"] == model for chunk in chunks)
            content = _assemble_content(chunks)
            assert content
            assert marker in content
    finally:
        await _delete_team(db_pool, team["team_id"])
