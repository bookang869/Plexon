"""Window-based alert evaluator (PRD Core Feature 4, ADR-014, ADR-027): the
two alert triggers with no single triggering event -- provider error rate and
latency P99 -- evaluated on a periodic tick against a rolling window of REAL
request outcomes recorded by gateway/routes.py. Deliberately independent of
gateway/resilience/health_check.py's ping-based window (ADR-025):
out-of-band health-check pings don't see the mocks' per-request fault
injection, so reusing that window would make these alerts blind to exactly
the outages this project's demo is built to show. No shared Redis keys, no
import in either direction.
"""

from __future__ import annotations

import asyncio
import json
import logging

from redis.asyncio import Redis

from gateway.config.loader import AlertingConfig, GatewayConfig
from gateway.observability.alerts import send_alert
from gateway.redis_client import get_redis

logger = logging.getLogger(__name__)

# Reflects real traffic volume rather than a fixed-cadence ping (unlike
# health_check.py's WINDOW_SIZE=5) -- large enough for a P99 to be a
# meaningful percentile (the worst ~1 sample of 50), small enough that a
# resolved outage ages out of the window within a couple dozen requests
# instead of lingering for hours.
WINDOW_SIZE = 50

_PROVIDERS = ("openai", "anthropic", "ollama")


def _recent_key(provider: str) -> str:
    return f"alert_window:{provider}:recent"


def _error_rate_breached_key(provider: str) -> str:
    return f"alerts:{provider}:error_rate_breached"


def _latency_breached_key(provider: str) -> str:
    return f"alerts:{provider}:latency_breached"


async def record_request_outcome(redis: Redis, provider: str, success: bool, latency_ms: float) -> None:
    key = _recent_key(provider)
    await redis.lpush(key, json.dumps({"success": success, "latency_ms": latency_ms}))
    await redis.ltrim(key, 0, WINDOW_SIZE - 1)


def _p99(latencies_ms: list[float]) -> float:
    """Nearest-rank percentile over the window -- small enough (WINDOW_SIZE)
    to sort in memory rather than reach for a streaming estimator.
    """
    ordered = sorted(latencies_ms)
    index = max(0, int(len(ordered) * 0.99) - 1)
    return ordered[index]


async def _evaluate_condition(
    redis: Redis,
    *,
    breached_key: str,
    currently_breached: bool,
    alert_type: str,
    message: str,
    context: dict,
) -> None:
    """Alerts only on a false->true transition of `currently_breached`, and
    clears the dedup key on recovery so a later re-breach can alert again --
    never on every tick while the condition merely continues to hold.
    """
    was_breached = bool(await redis.get(breached_key))
    if currently_breached and not was_breached:
        await redis.set(breached_key, "1")
        await send_alert(alert_type, message, context)
    elif not currently_breached and was_breached:
        await redis.delete(breached_key)


async def evaluate_provider(redis: Redis, provider: str, config: AlertingConfig) -> None:
    raw_window = await redis.lrange(_recent_key(provider), 0, WINDOW_SIZE - 1)
    if not raw_window:
        return
    window = [json.loads(entry) for entry in raw_window]

    error_rate = sum(1 for entry in window if not entry["success"]) / len(window)
    p99_latency_ms = _p99([entry["latency_ms"] for entry in window])

    await _evaluate_condition(
        redis,
        breached_key=_error_rate_breached_key(provider),
        currently_breached=error_rate > config.error_rate_threshold,
        alert_type="provider_error_rate",
        message=(
            f"provider={provider} error rate {error_rate:.0%} exceeds threshold "
            f"{config.error_rate_threshold:.0%} over the last {len(window)} requests"
        ),
        context={"provider": provider, "error_rate": error_rate, "window_size": len(window)},
    )

    await _evaluate_condition(
        redis,
        breached_key=_latency_breached_key(provider),
        currently_breached=p99_latency_ms > config.latency_p99_ms_threshold,
        alert_type="provider_latency_p99",
        message=(
            f"provider={provider} P99 latency {p99_latency_ms:.0f}ms exceeds threshold "
            f"{config.latency_p99_ms_threshold}ms over the last {len(window)} requests"
        ),
        context={"provider": provider, "p99_latency_ms": p99_latency_ms, "window_size": len(window)},
    )


async def run_alert_evaluator_loop(config: GatewayConfig) -> None:
    redis = get_redis()
    while True:
        await asyncio.sleep(config.alerting.evaluator_interval_seconds)
        for provider in _PROVIDERS:
            try:
                await evaluate_provider(redis, provider, config.alerting)
            except Exception:
                logger.exception("alert evaluation failed for provider=%s", provider)


def start_alert_evaluator_loop(config: GatewayConfig) -> asyncio.Task:
    return asyncio.create_task(run_alert_evaluator_loop(config))
