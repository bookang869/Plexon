"""Tests for gateway/resilience/circuit_breaker.py. Requires real Redis and
Postgres: `docker compose -f deploy/docker-compose.yml up -d redis postgres`.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from gateway.config.loader import CircuitBreakerConfig
from gateway.resilience import circuit_breaker
from gateway.resilience.circuit_breaker import (
    _failures_key,
    _opened_at_key,
    _probe_claimed_key,
    _state_key,
    check_breaker,
    record_failure,
    record_success,
)


def _config(*, failure_threshold=3, window_seconds=60, cooldown_seconds=1) -> CircuitBreakerConfig:
    return CircuitBreakerConfig(
        failure_threshold=failure_threshold,
        window_seconds=window_seconds,
        cooldown_seconds=cooldown_seconds,
    )


@pytest_asyncio.fixture
async def provider(redis_client, db_pool):
    name = f"test-provider-{uuid.uuid4().hex[:8]}"
    yield name
    await redis_client.delete(
        _state_key(name), _failures_key(name), _opened_at_key(name), _probe_claimed_key(name)
    )
    await db_pool.execute("DELETE FROM circuit_breaker_history WHERE provider = $1", name)


async def _open_breaker(redis_client, provider, config) -> None:
    for _ in range(config.failure_threshold):
        await record_failure(redis_client, provider, was_probe=False, config=config)


# --- closed state --------------------------------------------------------


@pytest.mark.asyncio
async def test_closed_state_allows_non_probe_traffic(redis_client, provider):
    config = _config()
    decision = await check_breaker(redis_client, provider, config)
    assert decision.allowed is True
    assert decision.is_probe is False


@pytest.mark.asyncio
async def test_failure_below_threshold_keeps_breaker_closed(redis_client, provider):
    config = _config(failure_threshold=3)
    await record_failure(redis_client, provider, was_probe=False, config=config)
    await record_failure(redis_client, provider, was_probe=False, config=config)

    decision = await check_breaker(redis_client, provider, config)
    assert decision.allowed is True
    assert decision.is_probe is False


# --- opening on threshold --------------------------------------------------


@pytest.mark.asyncio
async def test_failure_at_threshold_opens_breaker_and_writes_history(redis_client, db_pool, provider):
    config = _config(failure_threshold=3, cooldown_seconds=60)
    await _open_breaker(redis_client, provider, config)

    decision = await check_breaker(redis_client, provider, config)
    assert decision.allowed is False

    row = await db_pool.fetchrow(
        "SELECT * FROM circuit_breaker_history WHERE provider = $1 ORDER BY id DESC LIMIT 1",
        provider,
    )
    assert row is not None
    assert row["from_state"] == "closed"
    assert row["to_state"] == "open"
    assert row["reason"] == "failure_threshold_reached"


@pytest.mark.asyncio
async def test_open_before_cooldown_elapsed_denies(redis_client, provider):
    config = _config(failure_threshold=2, cooldown_seconds=60)
    await _open_breaker(redis_client, provider, config)

    decision = await check_breaker(redis_client, provider, config)
    assert decision.allowed is False
    assert decision.is_probe is False


# --- half-open transition ---------------------------------------------------


@pytest.mark.asyncio
async def test_open_after_cooldown_transitions_to_half_open_and_writes_history(
    redis_client, db_pool, provider
):
    config = _config(failure_threshold=2, cooldown_seconds=1)
    await _open_breaker(redis_client, provider, config)

    await asyncio.sleep(1.2)

    decision = await check_breaker(redis_client, provider, config)
    assert decision.allowed is True
    assert decision.is_probe is True

    row = await db_pool.fetchrow(
        "SELECT * FROM circuit_breaker_history WHERE provider = $1 ORDER BY id DESC LIMIT 1",
        provider,
    )
    assert row is not None
    assert row["from_state"] == "open"
    assert row["to_state"] == "half_open"
    assert row["reason"] == "cooldown_elapsed"


@pytest.mark.asyncio
async def test_concurrent_check_breaker_only_one_probe_winner(redis_client, provider):
    config = _config(failure_threshold=2, cooldown_seconds=1)
    await _open_breaker(redis_client, provider, config)

    await asyncio.sleep(1.2)

    decisions = await asyncio.gather(
        *[check_breaker(redis_client, provider, config) for _ in range(20)]
    )

    probes = [d for d in decisions if d.is_probe]
    non_probes = [d for d in decisions if not d.is_probe]
    assert len(probes) == 1
    assert probes[0].allowed is True
    assert all(d.allowed is False for d in non_probes)


# --- probe outcomes ----------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_success_closes_breaker_and_resets_failures(redis_client, db_pool, provider):
    config = _config(failure_threshold=2, cooldown_seconds=1)
    await _open_breaker(redis_client, provider, config)
    await asyncio.sleep(1.2)

    probe_decision = await check_breaker(redis_client, provider, config)
    assert probe_decision.is_probe is True

    await record_success(redis_client, provider, was_probe=True)

    decision = await check_breaker(redis_client, provider, config)
    assert decision.allowed is True
    assert decision.is_probe is False

    row = await db_pool.fetchrow(
        "SELECT * FROM circuit_breaker_history WHERE provider = $1 ORDER BY id DESC LIMIT 1",
        provider,
    )
    assert row["from_state"] == "half_open"
    assert row["to_state"] == "closed"
    assert row["reason"] == "probe_succeeded"

    # failures reset -- driving one below threshold again must stay closed.
    await record_failure(redis_client, provider, was_probe=False, config=config)
    decision = await check_breaker(redis_client, provider, config)
    assert decision.allowed is True
    assert decision.is_probe is False


@pytest.mark.asyncio
async def test_probe_failure_reopens_immediately_and_resets_cooldown(redis_client, db_pool, provider):
    config = _config(failure_threshold=5, cooldown_seconds=1)
    await _open_breaker(redis_client, provider, config)
    await asyncio.sleep(1.2)

    probe_decision = await check_breaker(redis_client, provider, config)
    assert probe_decision.is_probe is True

    await record_failure(redis_client, provider, was_probe=True, config=config)

    # Reopened immediately -- decisive on its own, no waiting for
    # failure_threshold again.
    decision = await check_breaker(redis_client, provider, config)
    assert decision.allowed is False

    row = await db_pool.fetchrow(
        "SELECT * FROM circuit_breaker_history WHERE provider = $1 ORDER BY id DESC LIMIT 1",
        provider,
    )
    assert row["from_state"] == "half_open"
    assert row["to_state"] == "open"
    assert row["reason"] == "probe_failed"

    # opened_at reset to now -- cooldown timing restarts, so even though the
    # original cooldown window has long since elapsed relative to the first
    # opening, immediately after the probe failure it must still be denied.
    decision = await check_breaker(redis_client, provider, config)
    assert decision.allowed is False


# --- postgres resilience ------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_write_failure_does_not_raise_or_corrupt_redis_state(
    redis_client, provider, monkeypatch
):
    config = _config(failure_threshold=2, cooldown_seconds=60)

    def _broken_get_pool():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(circuit_breaker, "get_pool", _broken_get_pool)

    await record_failure(redis_client, provider, was_probe=False, config=config)
    await record_failure(redis_client, provider, was_probe=False, config=config)

    decision = await check_breaker(redis_client, provider, config)
    assert decision.allowed is False
