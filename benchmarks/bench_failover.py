"""Failover benchmark (step 3 of phases/benchmarks; TRD §10's "circuit
breaker opens/closes correctly under injected faults", CLAUDE.md's CRITICAL
retry rule). Runs against the real docker-compose stack -- real `anthropic`
provider name, real breaker state in Redis/Postgres -- same shared-state
caution tests/integration/test_concurrent_resilience.py's module docstring
calls out: this file mutates the shared `anthropic` provider's circuit
breaker, so the `anthropic_breaker` fixture below resets it before and after
every test to keep other test files/benchmarks that touch `anthropic`
order-independent.

The reliability/switch-latency test runs multiple independent outage trials
(`_OUTAGE_TRIALS`), resetting the breaker to closed between each, so the
reported reliability percentage is backed by a larger pooled sample than any
single 20-request wave.

Fault injection uses the magic model-name suffix (ADR-025):
`claude-sonnet--fault-error` always returns an instant HTTP 500 from
mock-anthropic, classified RetryableProviderError (gateway/providers/
errors.py) -- a 429 would classify identically, so no separate fault-type
model is needed to exercise the retry -> fallback -> circuit-breaker path
(see gateway/providers/errors.py's _NON_RETRYABLE_STATUS_CODES).
"""

from __future__ import annotations

import asyncio
import os
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

# Independent outage trials the reliability/switch-latency test runs, each
# starting from a freshly-closed breaker -- env-overridable for local
# tuning without editing the file.
_OUTAGE_TRIALS = int(os.environ.get("PLEXON_BENCHMARK_FAILOVER_TRIALS", "5"))


def _ensure_config_loaded() -> None:
    global _config_loaded
    if not _config_loaded:
        start_config_watcher()
        _config_loaded = True


async def _reset_anthropic_breaker(redis_client, db_pool) -> None:
    """Deletes the shared `anthropic` provider's breaker state (Redis keys +
    circuit_breaker_history rows) -- shared by the `anthropic_breaker`
    fixture and the per-trial reset in
    test_failover_reliability_and_switch_latency so every trial starts from
    a closed breaker.
    """
    await redis_client.delete(
        _state_key("anthropic"),
        _failures_key("anthropic"),
        _opened_at_key("anthropic"),
        _probe_claimed_key("anthropic"),
    )
    await db_pool.execute("DELETE FROM circuit_breaker_history WHERE provider = 'anthropic'")


@pytest_asyncio.fixture
async def anthropic_breaker(redis_client, db_pool):
    """Resets the shared `anthropic` provider's breaker (Redis state +
    history rows) before and after each test, same technique as
    tests/integration/test_concurrent_resilience.py's `anthropic_breaker`
    fixture.
    """
    await _reset_anthropic_breaker(redis_client, db_pool)
    yield
    await _reset_anthropic_breaker(redis_client, db_pool)


def _fault_payload() -> dict:
    return {"model": _FAULT_MODEL, "messages": [{"role": "user", "content": "benchmark failover"}]}


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_failover_reliability_and_switch_latency(
    benchmark_team, anthropic_breaker, redis_client, db_pool
):
    client = await gateway_client()
    headers = {"Authorization": f"Bearer {benchmark_team['api_key']}"}
    _TOTAL_REQUESTS = 20

    async def _make_request() -> LatencySample:
        started = time.monotonic()
        resp = await client.post("/v1/chat/completions", json=_fault_payload(), headers=headers)
        return LatencySample(
            started_at=started,
            elapsed_seconds=time.monotonic() - started,
            success=resp.status_code == 200 and resp.json().get("model") == _FALLBACK_MODEL,
        )

    per_trial: list[dict] = []
    pooled_successful_elapsed: list[float] = []
    total_success_count = 0
    total_requests_all_trials = 0

    try:
        for trial in range(_OUTAGE_TRIALS):
            # Breaker must start closed each trial -- otherwise a breaker
            # left open from trial N would make trial N+1's requests fail
            # differently (served by fallback without ever retrying the
            # primary), skewing switch-latency numbers for reasons unrelated
            # to reliability.
            await _reset_anthropic_breaker(redis_client, db_pool)

            samples = await run_concurrent(
                _make_request, concurrency=_TOTAL_REQUESTS, total_requests=_TOTAL_REQUESTS
            )
            successes = [s for s in samples if s.success]

            per_trial.append(
                {
                    "trial": trial,
                    "total_requests": len(samples),
                    "success_count": len(successes),
                    "reliability_pct": 100.0 * len(successes) / len(samples),
                }
            )
            pooled_successful_elapsed.extend(s.elapsed_seconds for s in successes)
            total_success_count += len(successes)
            total_requests_all_trials += len(samples)
    finally:
        await client.aclose()

    overall_reliability_pct = 100.0 * total_success_count / total_requests_all_trials
    switch_quantiles = percentiles(pooled_successful_elapsed)
    failover_switch_p95_seconds = switch_quantiles[0.95]

    result = BenchmarkResult(
        name="failover_reliability",
        metrics={
            "trials": _OUTAGE_TRIALS,
            "total_requests_all_trials": total_requests_all_trials,
            "overall_reliability_pct": overall_reliability_pct,
            "failover_switch_p50_seconds": switch_quantiles[0.5],
            "failover_switch_p95_seconds": failover_switch_p95_seconds,
            "failover_switch_p99_seconds": switch_quantiles[0.99],
            "per_trial": per_trial,
        },
        thresholds={
            "reliability_min_pct": thresholds.FAILOVER_RELIABILITY_MIN_PCT,
            "switch_max_seconds": thresholds.FAILOVER_SWITCH_MAX_SECONDS,
        },
        passed=(
            overall_reliability_pct >= thresholds.FAILOVER_RELIABILITY_MIN_PCT
            and failover_switch_p95_seconds < thresholds.FAILOVER_SWITCH_MAX_SECONDS
        ),
    )
    write_result(result)

    assert overall_reliability_pct >= thresholds.FAILOVER_RELIABILITY_MIN_PCT
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
