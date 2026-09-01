"""Tests for gateway/observability/alerts.py's send_alert sink (ADR-014,
ADR-027) and the two event-driven triggers wired into
circuit_breaker.py/budget.py. Requires real Redis and Postgres:
`docker compose -f deploy/docker-compose.yml up -d redis postgres`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio

from gateway.auth.team_auth import Team
from gateway.config.loader import CircuitBreakerConfig
from gateway.observability import alerts
from gateway.ratelimit import budget as budget_module
from gateway.ratelimit.budget import BudgetStatus, _alert_daily_key, alert_on_budget_warning
from gateway.resilience import circuit_breaker
from gateway.resilience.circuit_breaker import (
    _failures_key,
    _opened_at_key,
    _probe_claimed_key,
    _state_key,
    record_failure,
)


def _breaker_config(*, failure_threshold=3, window_seconds=60, cooldown_seconds=60) -> CircuitBreakerConfig:
    return CircuitBreakerConfig(
        failure_threshold=failure_threshold, window_seconds=window_seconds, cooldown_seconds=cooldown_seconds
    )


async def _open_breaker(redis_client, provider, config) -> None:
    for _ in range(config.failure_threshold):
        await record_failure(redis_client, provider, was_probe=False, config=config)


# --- send_alert sink -----------------------------------------------------


@pytest.mark.asyncio
async def test_send_alert_without_webhook_logs_and_writes_history(db_pool, monkeypatch, caplog):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    posted = False

    async def _fake_post(self, *args, **kwargs):
        nonlocal posted
        posted = True

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    alert_type = f"test-alert-{uuid.uuid4().hex[:8]}"
    with caplog.at_level("WARNING"):
        await alerts.send_alert(alert_type, "test message", {"provider": "openai"})

    assert not posted
    assert "test message" in caplog.text

    row = await db_pool.fetchrow(
        "SELECT * FROM alert_history WHERE alert_type = $1 ORDER BY id DESC LIMIT 1", alert_type
    )
    assert row is not None
    assert row["message"] == "test message"
    assert row["provider"] == "openai"

    await db_pool.execute("DELETE FROM alert_history WHERE alert_type = $1", alert_type)


@pytest.mark.asyncio
async def test_send_alert_with_webhook_posts(db_pool, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/fake")

    calls = []

    async def _fake_post(self, url, json=None, **kwargs):
        calls.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    alert_type = f"test-alert-{uuid.uuid4().hex[:8]}"
    await alerts.send_alert(alert_type, "test message", {"provider": "openai"})

    assert len(calls) == 1
    assert calls[0][0] == "https://hooks.slack.test/fake"

    await db_pool.execute("DELETE FROM alert_history WHERE alert_type = $1", alert_type)


@pytest.mark.asyncio
async def test_send_alert_webhook_post_failure_does_not_raise(db_pool, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/fake")

    async def _broken_post(self, *args, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "post", _broken_post)

    alert_type = f"test-alert-{uuid.uuid4().hex[:8]}"
    await alerts.send_alert(alert_type, "test message", {})  # must not raise

    row = await db_pool.fetchrow(
        "SELECT * FROM alert_history WHERE alert_type = $1 ORDER BY id DESC LIMIT 1", alert_type
    )
    assert row is not None

    await db_pool.execute("DELETE FROM alert_history WHERE alert_type = $1", alert_type)


# --- circuit breaker open trigger -----------------------------------------


@pytest_asyncio.fixture
async def breaker_provider(redis_client, db_pool):
    name = f"test-provider-{uuid.uuid4().hex[:8]}"
    yield name
    await redis_client.delete(
        _state_key(name), _failures_key(name), _opened_at_key(name), _probe_claimed_key(name)
    )
    await db_pool.execute("DELETE FROM circuit_breaker_history WHERE provider = $1", name)


@pytest.mark.asyncio
async def test_breaker_opening_triggers_exactly_one_circuit_breaker_open_alert(
    redis_client, breaker_provider, monkeypatch
):
    config = _breaker_config(failure_threshold=3)

    calls = []

    async def _fake_send_alert(alert_type, message, context):
        calls.append((alert_type, message, context))

    monkeypatch.setattr(circuit_breaker, "send_alert", _fake_send_alert)

    await _open_breaker(redis_client, breaker_provider, config)

    open_calls = [c for c in calls if c[0] == "circuit_breaker_open"]
    assert len(open_calls) == 1
    assert open_calls[0][2]["provider"] == breaker_provider


# --- budget-crosses-80% trigger --------------------------------------------


def _team(*, daily_budget_usd="10.00", monthly_budget_usd="200.00") -> Team:
    return Team(
        id=f"team-test-{uuid.uuid4().hex[:8]}",
        name="Budget Alert Test Team",
        allowed_models=["gpt-4o-mini"],
        rpm_limit=1000,
        tpm_limit=100_000,
        daily_budget_usd=Decimal(daily_budget_usd),
        monthly_budget_usd=Decimal(monthly_budget_usd),
        config={},
    )


@pytest_asyncio.fixture
async def alert_team(redis_client):
    team = _team()
    yield team
    await redis_client.delete(_alert_daily_key(team.id))


@pytest.mark.asyncio
async def test_budget_crossing_80_percent_alerts_once_per_day_and_again_next_day(
    redis_client, alert_team, monkeypatch
):
    calls = []

    async def _fake_send_alert(alert_type, message, context):
        calls.append((alert_type, context))

    monkeypatch.setattr(budget_module, "send_alert", _fake_send_alert)

    status = BudgetStatus(blocked=False, warning=True, daily_utilization=0.85, monthly_utilization=0.1)

    await alert_on_budget_warning(alert_team, status)
    await alert_on_budget_warning(alert_team, status)  # same day, still above 80% -- no re-alert

    assert len(calls) == 1
    assert calls[0][0] == "budget_warning"
    assert calls[0][1]["period"] == "daily"

    # Simulate the day rolling over by clearing the period-scoped dedup key
    # directly -- advancing the real clock isn't practical in a unit test.
    await redis_client.delete(_alert_daily_key(alert_team.id))
    await alert_on_budget_warning(alert_team, status)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_budget_alert_only_fires_for_the_period_that_actually_crossed(
    redis_client, alert_team, monkeypatch
):
    calls = []

    async def _fake_send_alert(alert_type, message, context):
        calls.append((alert_type, context))

    monkeypatch.setattr(budget_module, "send_alert", _fake_send_alert)

    # Over on daily, well under on monthly -- only the daily alert should fire.
    status = BudgetStatus(blocked=False, warning=True, daily_utilization=0.9, monthly_utilization=0.2)
    await alert_on_budget_warning(alert_team, status)

    assert len(calls) == 1
    assert calls[0][1]["period"] == "daily"
