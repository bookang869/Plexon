"""Tests for gateway/ratelimit/token_bucket.py. Requires real Redis:
`docker compose -f deploy/docker-compose.yml up -d redis`.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from gateway.ratelimit.token_bucket import check_and_consume, refund


def _key() -> str:
    return f"test:bucket:{uuid.uuid4().hex}"


@pytest.mark.asyncio
async def test_admits_up_to_capacity_then_denies(redis_client):
    key = _key()

    for _ in range(3):
        result = await check_and_consume(redis_client, key, capacity=3, refill_per_second=1)
        assert result.allowed is True

    result = await check_and_consume(redis_client, key, capacity=3, refill_per_second=1)
    assert result.allowed is False
    assert result.retry_after_seconds > 0


@pytest.mark.asyncio
async def test_refill_admits_previously_denied_request(redis_client):
    key = _key()

    for _ in range(2):
        result = await check_and_consume(redis_client, key, capacity=2, refill_per_second=5)
        assert result.allowed is True

    denied = await check_and_consume(redis_client, key, capacity=2, refill_per_second=5)
    assert denied.allowed is False

    await asyncio.sleep(0.3)

    result = await check_and_consume(redis_client, key, capacity=2, refill_per_second=5)
    assert result.allowed is True


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_over_admit(redis_client):
    key = _key()
    capacity = 10
    extra = 5

    results = await asyncio.gather(
        *[
            check_and_consume(redis_client, key, capacity=capacity, refill_per_second=0)
            for _ in range(capacity + extra)
        ]
    )

    allowed_count = sum(1 for r in results if r.allowed)
    denied_count = sum(1 for r in results if not r.allowed)
    assert allowed_count == capacity
    assert denied_count == extra


@pytest.mark.asyncio
async def test_refund_increases_remaining_capped_at_capacity(redis_client):
    key = _key()
    capacity = 5

    for _ in range(capacity):
        result = await check_and_consume(redis_client, key, capacity=capacity, refill_per_second=0)
        assert result.allowed is True

    denied = await check_and_consume(redis_client, key, capacity=capacity, refill_per_second=0)
    assert denied.allowed is False

    await refund(redis_client, key, capacity=capacity, amount=100)

    result = await check_and_consume(redis_client, key, capacity=capacity, refill_per_second=0)
    assert result.allowed is True
    assert result.remaining == pytest.approx(capacity - 1)


@pytest.mark.asyncio
async def test_variable_cost_deducted_correctly(redis_client):
    key = _key()

    first = await check_and_consume(
        redis_client, key, capacity=100, refill_per_second=0, cost=50
    )
    assert first.allowed is True
    assert first.remaining == pytest.approx(50)

    second = await check_and_consume(
        redis_client, key, capacity=100, refill_per_second=0, cost=50
    )
    assert second.allowed is True
    assert second.remaining == pytest.approx(0)

    third = await check_and_consume(redis_client, key, capacity=100, refill_per_second=0, cost=1)
    assert third.allowed is False
