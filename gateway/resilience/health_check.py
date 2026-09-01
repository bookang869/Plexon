"""Background provider health-check loop (PRD Core Feature 3, TRD §4.2's
`health:{provider}:{model}:status` key, `health_check.interval_seconds`
config). Populates the Operations dashboard and `provider_health_history`
only -- deliberately independent of `gateway/resilience/circuit_breaker.py`
(no shared Redis keys, no calls in either direction). The breaker reacts to
real request outcomes; this loop pings providers out-of-band and would report
a mocked provider as healthy throughout a per-request-triggered simulated
outage (ADR-025), so wiring the two together would make the breaker miss real
outages or close prematurely.

`ProviderAdapter.health_check()` is a single ping per provider, not per model
(gateway/providers/base.py) -- this loop pings once per provider per tick and
publishes that one result to every model the provider serves.
"""

from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum

import asyncpg
from redis.asyncio import Redis

from gateway.config.loader import GatewayConfig
from gateway.db import get_pool
from gateway.providers.base import ProviderAdapter
from gateway.providers.registry import get_adapter_for_provider
from gateway.redis_client import get_redis

logger = logging.getLogger(__name__)

WINDOW_SIZE = 5
LATENCY_DEGRADED_THRESHOLD_MS = 2000

_PROVIDERS = ("openai", "anthropic", "ollama")

_INSERT_HISTORY_ROW = """
    INSERT INTO provider_health_history (provider, model, status, error_rate, p99_latency_ms)
    VALUES ($1, NULL, $2, $3, $4)
"""


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


def _recent_key(provider: str) -> str:
    return f"health:{provider}:recent"


def _status_key(provider: str, model: str) -> str:
    return f"health:{provider}:{model}:status"


def _compute_state(window: list[dict]) -> HealthState:
    """`window` is most-recent-first, as produced by LPUSH+LRANGE."""
    latest = window[0]
    if not latest["healthy"]:
        return HealthState.DOWN

    if (
        len(window) < WINDOW_SIZE
        or (latest["latency_ms"] or 0) > LATENCY_DEGRADED_THRESHOLD_MS
        or any(not entry["healthy"] for entry in window[1:])
    ):
        return HealthState.DEGRADED

    return HealthState.HEALTHY


async def _record_history(
    pool: asyncpg.Pool, provider: str, state: HealthState, error_rate: float, p99_latency_ms: float | None
) -> None:
    """Best-effort history write, mirroring circuit_breaker.py's/budget.py's
    pattern -- never raises, never blocks the loop on Postgres being
    reachable.
    """
    try:
        await pool.execute(_INSERT_HISTORY_ROW, provider, state.value, error_rate, p99_latency_ms)
    except Exception:
        logger.exception("failed to write provider_health_history row for provider=%s", provider)


async def check_provider_health(
    redis: Redis,
    pool: asyncpg.Pool,
    provider: str,
    adapter: ProviderAdapter,
    config: GatewayConfig,
) -> HealthState:
    result = await adapter.health_check()

    recent_key = _recent_key(provider)
    await redis.lpush(recent_key, json.dumps({"healthy": result.healthy, "latency_ms": result.latency_ms}))
    await redis.ltrim(recent_key, 0, WINDOW_SIZE - 1)

    raw_window = await redis.lrange(recent_key, 0, WINDOW_SIZE - 1)
    window = [json.loads(entry) for entry in raw_window]

    state = _compute_state(window)

    status_ttl = config.health_check.interval_seconds * 3
    models = getattr(config.providers, provider).models
    for model in models:
        await redis.set(_status_key(provider, model), state.value, ex=status_ttl)

    error_rate = sum(1 for entry in window if not entry["healthy"]) / len(window)
    p99_latency_ms = window[0]["latency_ms"] if window[0]["healthy"] else None
    await _record_history(pool, provider, state, error_rate, p99_latency_ms)

    return state


async def run_health_check_loop(config: GatewayConfig) -> None:
    redis = get_redis()
    pool = get_pool()
    while True:
        await asyncio.sleep(config.health_check.interval_seconds)
        for provider in _PROVIDERS:
            try:
                adapter = get_adapter_for_provider(provider, config)
                await check_provider_health(redis, pool, provider, adapter, config)
            except Exception:
                logger.exception("health check failed for provider=%s", provider)


def start_health_check_loop(config: GatewayConfig) -> asyncio.Task:
    return asyncio.create_task(run_health_check_loop(config))
