"""Tests for gateway/resilience/health_check.py (PRD Core Feature 3, TRD §4.2).
Real Redis/Postgres (`docker compose -f deploy/docker-compose.yml up -d redis
postgres`); stub adapters with a controllable `health_check()` return, same
technique as test_orchestrator.py's ScriptedAttempt -- `check_provider_health`
is tested directly, one tick at a time, matching how config/loader.py's
`_watch_loop` is never directly tested either (only `reload_config()` is).

Provider name is fixed to "openai" (not a random uuid like
test_circuit_breaker.py's `provider` fixture) because
GatewayConfig.providers is a fixed-field model (openai/anthropic/ollama), not
an arbitrary dict -- check_provider_health reads config.providers.<provider>
directly.
"""

from __future__ import annotations

import ast
import os

import pytest
import pytest_asyncio
import yaml

from gateway.config.loader import GatewayConfig
from gateway.providers.base import HealthStatus
from gateway.resilience.health_check import (
    WINDOW_SIZE,
    HealthState,
    _recent_key,
    _status_key,
    check_provider_health,
)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "test_config.yaml")

with open(_CONFIG_PATH) as f:
    _CONFIG = GatewayConfig.model_validate(yaml.safe_load(f))

_PROVIDER = "openai"
_MODELS = ("gpt-4o", "gpt-4o-mini")


class _StubAdapter:
    """Ignores the real adapter shape entirely -- returns a scripted
    HealthStatus per call, popped in order.
    """

    def __init__(self, results: list[HealthStatus]):
        self._results = list(results)

    async def health_check(self) -> HealthStatus:
        return self._results.pop(0)


class _BrokenPool:
    async def execute(self, *args, **kwargs):
        raise RuntimeError("db unavailable")


def _healthy(latency_ms: float = 100.0) -> HealthStatus:
    return HealthStatus(provider=_PROVIDER, healthy=True, latency_ms=latency_ms)


def _unhealthy() -> HealthStatus:
    return HealthStatus(provider=_PROVIDER, healthy=False, error="boom")


@pytest_asyncio.fixture
async def clean_health_state(redis_client, db_pool):
    yield _PROVIDER
    keys = [_recent_key(_PROVIDER)] + [_status_key(_PROVIDER, m) for m in _MODELS]
    await redis_client.delete(*keys)
    await db_pool.execute("DELETE FROM provider_health_history WHERE provider = $1", _PROVIDER)


async def _tick_n_times(redis_client, db_pool, adapter, n: int) -> HealthState:
    state = None
    for _ in range(n):
        state = await check_provider_health(redis_client, db_pool, _PROVIDER, adapter, _CONFIG)
    assert state is not None
    return state


# --- status computation ------------------------------------------------------


@pytest.mark.asyncio
async def test_full_healthy_window_under_threshold_computes_healthy(
    redis_client, db_pool, clean_health_state
):
    adapter = _StubAdapter([_healthy() for _ in range(WINDOW_SIZE)])
    state = await _tick_n_times(redis_client, db_pool, adapter, WINDOW_SIZE)
    assert state == HealthState.HEALTHY


@pytest.mark.asyncio
async def test_most_recent_failure_computes_down_regardless_of_prior_ticks(
    redis_client, db_pool, clean_health_state
):
    results = [_healthy() for _ in range(WINDOW_SIZE - 1)] + [_unhealthy()]
    adapter = _StubAdapter(results)
    state = await _tick_n_times(redis_client, db_pool, adapter, WINDOW_SIZE)
    assert state == HealthState.DOWN


@pytest.mark.asyncio
async def test_partial_window_computes_degraded_even_if_all_succeeded(
    redis_client, db_pool, clean_health_state
):
    adapter = _StubAdapter([_healthy() for _ in range(WINDOW_SIZE - 1)])
    state = await _tick_n_times(redis_client, db_pool, adapter, WINDOW_SIZE - 1)
    assert state == HealthState.DEGRADED


@pytest.mark.asyncio
async def test_full_window_over_latency_threshold_computes_degraded(
    redis_client, db_pool, clean_health_state
):
    results = [_healthy() for _ in range(WINDOW_SIZE - 1)] + [_healthy(latency_ms=3000.0)]
    adapter = _StubAdapter(results)
    state = await _tick_n_times(redis_client, db_pool, adapter, WINDOW_SIZE)
    assert state == HealthState.DEGRADED


@pytest.mark.asyncio
async def test_full_window_with_earlier_failure_computes_degraded(
    redis_client, db_pool, clean_health_state
):
    results = (
        [_healthy() for _ in range(WINDOW_SIZE - 2)] + [_unhealthy()] + [_healthy()]
    )
    adapter = _StubAdapter(results)
    state = await _tick_n_times(redis_client, db_pool, adapter, WINDOW_SIZE)
    assert state == HealthState.DEGRADED


# --- redis publishing ---------------------------------------------------------


@pytest.mark.asyncio
async def test_status_published_to_every_configured_model(redis_client, db_pool, clean_health_state):
    adapter = _StubAdapter([_healthy()])
    await check_provider_health(redis_client, db_pool, _PROVIDER, adapter, _CONFIG)

    for model in _MODELS:
        raw = await redis_client.get(_status_key(_PROVIDER, model))
        assert raw is not None
        assert raw.decode() == HealthState.DEGRADED.value  # single tick -- partial window


# --- postgres history ----------------------------------------------------------


@pytest.mark.asyncio
async def test_history_row_written_each_tick_with_correct_status_and_error_rate(
    redis_client, db_pool, clean_health_state
):
    adapter = _StubAdapter([_healthy() for _ in range(WINDOW_SIZE - 1)] + [_unhealthy()])
    for _ in range(WINDOW_SIZE):
        await check_provider_health(redis_client, db_pool, _PROVIDER, adapter, _CONFIG)

    row = await db_pool.fetchrow(
        "SELECT * FROM provider_health_history WHERE provider = $1 ORDER BY id DESC LIMIT 1",
        _PROVIDER,
    )
    assert row is not None
    assert row["model"] is None
    assert row["status"] == HealthState.DOWN.value
    assert float(row["error_rate"]) == pytest.approx(1 / WINDOW_SIZE)
    assert row["p99_latency_ms"] is None


@pytest.mark.asyncio
async def test_postgres_failure_does_not_raise_or_corrupt_redis_writes(
    redis_client, clean_health_state
):
    adapter = _StubAdapter([_healthy()])
    state = await check_provider_health(redis_client, _BrokenPool(), _PROVIDER, adapter, _CONFIG)

    assert state == HealthState.DEGRADED
    for model in _MODELS:
        raw = await redis_client.get(_status_key(_PROVIDER, model))
        assert raw is not None
        assert raw.decode() == HealthState.DEGRADED.value


# --- decoupling from circuit_breaker.py ---------------------------------------


def _imported_module_names(path: str) -> set[str]:
    with open(path) as f:
        tree = ast.parse(f.read())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_health_check_and_circuit_breaker_share_no_import_relationship():
    resilience_dir = os.path.join(os.path.dirname(__file__), "..", "gateway", "resilience")
    health_check_imports = _imported_module_names(os.path.join(resilience_dir, "health_check.py"))
    circuit_breaker_imports = _imported_module_names(os.path.join(resilience_dir, "circuit_breaker.py"))

    assert not any("circuit_breaker" in m for m in health_check_imports)
    assert not any("health_check" in m for m in circuit_breaker_imports)
