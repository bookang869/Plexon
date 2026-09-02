"""Tests for gateway/observability/alert_evaluator.py (ADR-025, ADR-027).
Requires real Redis: `docker compose -f deploy/docker-compose.yml up -d
redis postgres`. send_alert is monkeypatched throughout so these tests never
depend on a real `provider` row existing in Postgres.
"""

from __future__ import annotations

import ast
import os
import uuid

import pytest
import pytest_asyncio

from gateway.config.loader import AlertingConfig
from gateway.observability import alert_evaluator
from gateway.observability.alert_evaluator import (
    WINDOW_SIZE,
    _error_rate_breached_key,
    _latency_breached_key,
    _recent_key,
    evaluate_provider,
    record_request_outcome,
)


def _config(*, error_rate_threshold=0.3, latency_p99_ms_threshold=1000) -> AlertingConfig:
    return AlertingConfig(
        error_rate_threshold=error_rate_threshold,
        latency_p99_ms_threshold=latency_p99_ms_threshold,
        evaluator_interval_seconds=30,
    )


@pytest_asyncio.fixture
async def provider(redis_client):
    name = f"test-provider-{uuid.uuid4().hex[:8]}"
    yield name
    await redis_client.delete(
        _recent_key(name), _error_rate_breached_key(name), _latency_breached_key(name)
    )


@pytest.fixture
def alert_calls(monkeypatch):
    calls = []

    async def _fake_send_alert(alert_type, message, context):
        calls.append((alert_type, context))

    monkeypatch.setattr(alert_evaluator, "send_alert", _fake_send_alert)
    return calls


async def _fill_window(redis_client, provider, *, failures, successes, latency_ms=100.0):
    for _ in range(failures):
        await record_request_outcome(redis_client, provider, success=False, latency_ms=latency_ms)
    for _ in range(successes):
        await record_request_outcome(redis_client, provider, success=True, latency_ms=latency_ms)


# --- record_request_outcome is the window's only feed -------------------------


@pytest.mark.asyncio
async def test_record_request_outcome_feeds_the_window(redis_client, provider):
    await record_request_outcome(redis_client, provider, success=True, latency_ms=50.0)
    raw = await redis_client.lrange(_recent_key(provider), 0, WINDOW_SIZE - 1)
    assert len(raw) == 1


def test_alert_evaluator_has_no_import_of_health_check():
    """Same decoupling technique as test_health_check.py's
    test_health_check_and_circuit_breaker_share_no_import_relationship --
    the window-based evaluator must be fed only by real request outcomes
    (ADR-025), never by health_check.py's out-of-band ping window.
    """
    resilience_dir = os.path.join(os.path.dirname(__file__), "..", "gateway", "resilience")
    observability_dir = os.path.join(os.path.dirname(__file__), "..", "gateway", "observability")
    assert os.path.exists(os.path.join(resilience_dir, "health_check.py"))

    with open(os.path.join(observability_dir, "alert_evaluator.py")) as f:
        tree = ast.parse(f.read())

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert not any("health_check" in m for m in modules)


# --- error-rate breach transition ----------------------------------------------


@pytest.mark.asyncio
async def test_error_rate_breach_alerts_once_then_not_again_while_still_breached(
    redis_client, provider, alert_calls
):
    config = _config(error_rate_threshold=0.3)
    await _fill_window(redis_client, provider, failures=5, successes=5)  # 50% > 30%

    await evaluate_provider(redis_client, provider, config)
    await evaluate_provider(redis_client, provider, config)  # still breached -- no re-alert

    error_rate_calls = [c for c in alert_calls if c[0] == "provider_error_rate"]
    assert len(error_rate_calls) == 1
    assert error_rate_calls[0][1]["provider"] == provider


@pytest.mark.asyncio
async def test_error_rate_breach_alerts_again_after_recovering_then_re_breaching(
    redis_client, provider, alert_calls
):
    config = _config(error_rate_threshold=0.3)

    await _fill_window(redis_client, provider, failures=5, successes=5)
    await evaluate_provider(redis_client, provider, config)  # breach #1

    await redis_client.delete(_recent_key(provider))
    await _fill_window(redis_client, provider, failures=0, successes=10)
    await evaluate_provider(redis_client, provider, config)  # drops below threshold -- clears dedup state

    await redis_client.delete(_recent_key(provider))
    await _fill_window(redis_client, provider, failures=5, successes=5)
    await evaluate_provider(redis_client, provider, config)  # breach #2

    error_rate_calls = [c for c in alert_calls if c[0] == "provider_error_rate"]
    assert len(error_rate_calls) == 2


# --- latency P99 breach ---------------------------------------------------------


@pytest.mark.asyncio
async def test_latency_p99_breach_alerts_once_then_not_again_while_still_breached(
    redis_client, provider, alert_calls
):
    config = _config(latency_p99_ms_threshold=1000)
    await _fill_window(redis_client, provider, failures=0, successes=10, latency_ms=2000.0)

    await evaluate_provider(redis_client, provider, config)
    await evaluate_provider(redis_client, provider, config)

    latency_calls = [c for c in alert_calls if c[0] == "provider_latency_p99"]
    assert len(latency_calls) == 1


# --- empty window ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_provider_on_empty_window_is_a_noop(redis_client, provider, alert_calls):
    await evaluate_provider(redis_client, provider, _config())
    assert alert_calls == []
