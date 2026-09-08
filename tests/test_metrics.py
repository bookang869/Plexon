"""Tests for gateway/observability/metrics.py wiring (TRD §8, ADR-026).
Drives real requests through the FastAPI app (same `httpx.AsyncClient` +
`seeded_team` pattern as `tests/test_routing.py`) and asserts against
`prometheus_client.REGISTRY`. Requires real Redis and Postgres:
`docker compose -f deploy/docker-compose.yml up -d redis postgres mock-openai
mock-anthropic`.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from prometheus_client import REGISTRY

from gateway.config.loader import CircuitBreakerConfig, start_config_watcher
from gateway.main import app
from gateway.providers.errors import RetryableProviderError
from gateway.resilience.circuit_breaker import (
    _failures_key,
    _opened_at_key,
    _probe_claimed_key,
    _state_key,
    record_failure,
)

_config_loaded = False

_REAL_PROVIDERS = ("openai", "anthropic", "ollama")


@pytest_asyncio.fixture(autouse=True)
async def _clean_real_provider_breaker_state(redis_client, db_pool):
    """The fallback/exhausted-candidates tests below drive real requests
    through the orchestrator using the real provider names (openai,
    anthropic, ollama), which record failures against their real
    circuit-breaker keys in the shared Redis instance -- same reasoning as
    tests/test_orchestrator.py's `_clean_breaker_state`. Without this,
    left-over failure counts leak into other test files that reuse these
    same provider names and can spuriously open the breaker for them.
    """
    yield
    for provider in _REAL_PROVIDERS:
        await redis_client.delete(
            _state_key(provider), _failures_key(provider), _opened_at_key(provider), _probe_claimed_key(provider)
        )
    await db_pool.execute(
        "DELETE FROM circuit_breaker_history WHERE provider = ANY($1)", list(_REAL_PROVIDERS)
    )


@pytest_asyncio.fixture
async def client(db_pool, redis_client):
    global _config_loaded
    if not _config_loaded:
        start_config_watcher()
        _config_loaded = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as ac:
        yield ac


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _sample(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# --- successful non-streaming request ---------------------------------------


@pytest.mark.asyncio
async def test_successful_request_increments_request_token_and_cost_metrics(client, seeded_team):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi there"}]},
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 200
    body = resp.json()

    assert (
        _sample(
            "gateway_requests_total",
            {"team": seeded_team["team_id"], "model": body["model"], "provider": "openai"},
        )
        == 1
    )
    assert (
        _sample(
            "gateway_tokens_total", {"team": seeded_team["team_id"], "direction": "input"}
        )
        == body["usage"]["prompt_tokens"]
    )
    assert (
        _sample(
            "gateway_tokens_total", {"team": seeded_team["team_id"], "direction": "output"}
        )
        == body["usage"]["completion_tokens"]
    )
    assert _sample("gateway_cost_usd_total", {"team": seeded_team["team_id"]}) > 0


# --- fallback -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_served_request_attributes_metrics_to_serving_provider(client, seeded_team):
    """fast_tier (tests/fixtures/test_config.yaml): [anthropic:claude-sonnet,
    openai:gpt-4o-mini, ollama:llama3] -- same stub-adapter technique as
    tests/test_routing.py's test_primary_failure_falls_back_to_next_provider_in_chain.
    """
    from gateway.providers import registry as provider_registry

    class _AlwaysFailsAdapter:
        async def chat_completion(self, request):
            raise RetryableProviderError("stub: primary always fails")

    provider_registry._adapters["anthropic"] = _AlwaysFailsAdapter()

    before = _sample(
        "gateway_fallback_triggered_total", {"from_provider": "anthropic", "to_provider": "openai"}
    )

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "gpt-4o-mini"

    after = _sample(
        "gateway_fallback_triggered_total", {"from_provider": "anthropic", "to_provider": "openai"}
    )
    assert after == before + 1

    assert (
        _sample(
            "gateway_requests_total",
            {"team": seeded_team["team_id"], "model": "gpt-4o-mini", "provider": "openai"},
        )
        == 1
    )
    assert _sample("gateway_cost_usd_total", {"team": seeded_team["team_id"]}) > 0


# --- every candidate exhausted -------------------------------------------------


@pytest.mark.asyncio
async def test_every_candidate_exhausted_increments_errors_total(client, seeded_team):
    """Stubs every provider in the fast_tier chain to always fail, forcing
    `create_chat_completion`'s RetryableProviderError except block.
    """
    from gateway.providers import registry as provider_registry

    class _AlwaysFailsAdapter:
        async def chat_completion(self, request):
            raise RetryableProviderError("stub: always fails")

    for provider in ("anthropic", "openai", "ollama"):
        provider_registry._adapters[provider] = _AlwaysFailsAdapter()

    before = _sample(
        "gateway_errors_total",
        {
            "team": seeded_team["team_id"],
            "model": "claude-sonnet",
            "provider": "anthropic",
            "error_type": "retryable",
        },
    )

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 503

    after = _sample(
        "gateway_errors_total",
        {
            "team": seeded_team["team_id"],
            "model": "claude-sonnet",
            "provider": "anthropic",
            "error_type": "retryable",
        },
    )
    assert after == before + 1


# --- circuit breaker -----------------------------------------------------------


@pytest_asyncio.fixture
async def breaker_provider(redis_client, db_pool):
    name = f"test-provider-{uuid.uuid4().hex[:8]}"
    yield name
    await redis_client.delete(
        _state_key(name), _failures_key(name), _opened_at_key(name), _probe_claimed_key(name)
    )
    await db_pool.execute("DELETE FROM circuit_breaker_history WHERE provider = $1", name)


@pytest.mark.asyncio
async def test_breaker_opening_increments_transitions_and_sets_state_gauge(
    redis_client, breaker_provider
):
    config = CircuitBreakerConfig(failure_threshold=3, window_seconds=60, cooldown_seconds=60)

    for _ in range(config.failure_threshold):
        await record_failure(redis_client, breaker_provider, was_probe=False, config=config)

    assert (
        _sample(
            "gateway_circuit_breaker_transitions_total",
            {"provider": breaker_provider, "from_state": "closed", "to_state": "open"},
        )
        == 1
    )
    assert _sample("gateway_circuit_breaker_state", {"provider": breaker_provider}) == 2


# --- gateway_overhead_seconds ---------------------------------------------------


@pytest.mark.asyncio
async def test_successful_request_increments_overhead_histogram(client, seeded_team):
    before_count = _sample("gateway_overhead_seconds_count", {"route": "chat_completion"})
    before_sum = _sample("gateway_overhead_seconds_sum", {"route": "chat_completion"})

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi there"}]},
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 200

    after_count = _sample("gateway_overhead_seconds_count", {"route": "chat_completion"})
    after_sum = _sample("gateway_overhead_seconds_sum", {"route": "chat_completion"})
    assert after_count == before_count + 1
    assert after_sum > before_sum


# --- /metrics endpoint ---------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_registry_output(client):
    resp = await client.get("/metrics", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "gateway_requests_total" in resp.text
