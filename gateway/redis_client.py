"""Redis connection client (ADR-004: hot-path state -- rate-limit token
buckets, circuit-breaker state, provider health status, spend counter).
"""

from __future__ import annotations

import os

import redis.asyncio as redis

_client: redis.Redis | None = None


async def init_redis() -> None:
    global _client
    url = os.environ["PLEXON_REDIS_URL"]
    _client = redis.from_url(url)


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_redis() -> redis.Redis:
    if _client is None:
        raise RuntimeError("redis client not initialized; call init_redis() first")
    return _client
