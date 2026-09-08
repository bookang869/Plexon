"""Failover benchmark (step 3 of phases/benchmarks; TRD §10's "circuit
breaker opens/closes correctly under injected faults", CLAUDE.md's CRITICAL
retry rule). Runs against the real docker-compose stack -- real `anthropic`
provider name, real breaker state in Redis/Postgres -- same shared-state
caution tests/integration/test_concurrent_resilience.py's module docstring
calls out: this file mutates the shared `anthropic` provider's circuit
breaker, so the `anthropic_breaker` fixture below resets it before and after
every test to keep other test files/benchmarks that touch `anthropic`
order-independent.

Fault injection uses the magic model-name suffix (ADR-025):
`claude-sonnet--fault-error` always returns an instant HTTP 500 from
mock-anthropic, classified RetryableProviderError (gateway/providers/
errors.py) -- a 429 would classify identically, so no separate fault-type
model is needed to exercise the retry -> fallback -> circuit-breaker path
(see gateway/providers/errors.py's _NON_RETRYABLE_STATUS_CODES).
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from benchmarks import thresholds
from benchmarks.common import (
    BenchmarkResult,
    LatencySample,
    gateway_client,
    percentiles,
    run_concurrent,
    write_result,
)
from gateway.config.loader import get_config, start_config_watcher
from gateway.resilience.circuit_breaker import (
    _failures_key,
    _opened_at_key,
    _probe_claimed_key,
    _state_key,
)

_FAULT_MODEL = "claude-sonnet--fault-error"
_HEALTHY_MODEL = "claude-sonnet"
_FALLBACK_MODEL = "gpt-4o-mini"

_config_loaded = False


def _ensure_config_loaded() -> None:
    global _config_loaded
    if not _config_loaded:
        start_config_watcher()
        _config_loaded = True


@pytest_asyncio.fixture
async def anthropic_breaker(redis_client, db_pool):
    """Resets the shared `anthropic` provider's breaker (Redis state +
    history rows) before and after each test, same technique as
    tests/integration/test_concurrent_resilience.py's `anthropic_breaker`
    fixture.
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


def _fault_payload() -> dict:
    return {"model": _FAULT_MODEL, "messages": [{"role": "user", "content": "benchmark failover"}]}


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_failover_reliability_and_switch_latency(benchmark_team, anthropic_breaker):
    client = await gateway_client()
    headers = {"Authorization": f"Bearer {benchmark_team['api_key']}"}
    try:
        _TOTAL_REQUESTS = 20

        async def _make_request() -> LatencySample:
            started = time.monotonic()
            resp = await client.post("/v1/chat/completions", json=_fault_payload(), headers=headers)
            return LatencySample(
                started_at=started,
                elapsed_seconds=time.monotonic() - started,
                success=resp.status_code == 200 and resp.json().get("model") == _FALLBACK_MODEL,
            )

        samples = await run_concurrent(
            _make_request, concurrency=_TOTAL_REQUESTS, total_requests=_TOTAL_REQUESTS
        )
    finally:
        await client.aclose()

    successes = [s for s in samples if s.success]
    reliability_pct = 100.0 * len(successes) / len(samples)
    switch_quantiles = percentiles([s.elapsed_seconds for s in successes])
    failover_switch_p95_seconds = switch_quantiles[0.95]

    result = BenchmarkResult(
        name="failover_reliability",
        metrics={
            "total_requests": len(samples),
            "reliability_pct": reliability_pct,
            "failover_switch_p50_seconds": switch_quantiles[0.5],
            "failover_switch_p95_seconds": failover_switch_p95_seconds,
            "failover_switch_p99_seconds": switch_quantiles[0.99],
        },
        thresholds={
            "reliability_min_pct": thresholds.FAILOVER_RELIABILITY_MIN_PCT,
            "switch_max_seconds": thresholds.FAILOVER_SWITCH_MAX_SECONDS,
        },
        passed=(
            reliability_pct >= thresholds.FAILOVER_RELIABILITY_MIN_PCT
            and failover_switch_p95_seconds < thresholds.FAILOVER_SWITCH_MAX_SECONDS
        ),
    )
    write_result(result)

    assert reliability_pct >= thresholds.FAILOVER_RELIABILITY_MIN_PCT
    assert failover_switch_p95_seconds < thresholds.FAILOVER_SWITCH_MAX_SECONDS


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_circuit_breaker_recovery_latency(benchmark_team, anthropic_breaker, db_pool):
    _ensure_config_loaded()
    config = get_config()
    breaker_config = config.circuit_breaker

    client = await gateway_client()
    headers = {"Authorization": f"Bearer {benchmark_team['api_key']}"}
    try:
        # Comfortably exceeds failure_threshold within window_seconds -- each
        # concurrent request contributes at most one recorded primary failure
        # (record_failure is called once per exhausted candidate, after
        # tenacity's retries are spent, not once per retry attempt).
        opening_requests = breaker_config.failure_threshold + 5

        responses = await asyncio.gather(
            *[
                client.post("/v1/chat/completions", json=_fault_payload(), headers=headers)
                for _ in range(opening_requests)
            ]
        )
        assert all(r.status_code == 200 for r in responses)

        opened_row = await db_pool.fetchrow(
            """
            SELECT created_at FROM circuit_breaker_history
            WHERE provider = 'anthropic' AND from_state = 'closed' AND to_state = 'open'
              AND reason = 'failure_threshold_reached'
            ORDER BY id DESC LIMIT 1
            """
        )
        assert opened_row is not None, "breaker did not open under injected load"
        t_open = opened_row["created_at"]

        await asyncio.sleep(breaker_config.cooldown_seconds)

        probe_payload = {
            "model": _HEALTHY_MODEL,
            "messages": [{"role": "user", "content": "benchmark recovery probe"}],
        }
        probe_resp = await client.post("/v1/chat/completions", json=probe_payload, headers=headers)
        assert probe_resp.status_code == 200
        assert probe_resp.json()["model"] == _HEALTHY_MODEL
    finally:
        await client.aclose()

    closed_row = await db_pool.fetchrow(
        """
        SELECT created_at FROM circuit_breaker_history
        WHERE provider = 'anthropic' AND from_state = 'half_open' AND to_state = 'closed'
          AND reason = 'probe_succeeded'
        ORDER BY id DESC LIMIT 1
        """
    )
    assert closed_row is not None, "breaker did not close after the healthy probe"
    t_closed = closed_row["created_at"]

    recovery_seconds = (t_closed - t_open).total_seconds()
    max_recovery_seconds = breaker_config.cooldown_seconds + thresholds.RECOVERY_MAX_SECONDS_OVER_COOLDOWN

    result = BenchmarkResult(
        name="failover_recovery",
        metrics={
            "recovery_seconds": recovery_seconds,
            "cooldown_seconds": breaker_config.cooldown_seconds,
        },
        thresholds={"max_recovery_seconds": max_recovery_seconds},
        passed=recovery_seconds < max_recovery_seconds,
    )
    write_result(result)

    assert recovery_seconds < max_recovery_seconds
