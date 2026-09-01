"""Tests for gateway/resilience/orchestrator.py (TRD §3 steps 6-7, PRD Core
Feature 3, CLAUDE.md's CRITICAL retry rule). Real Redis for breaker state
(`docker compose -f deploy/docker-compose.yml up -d redis postgres`); stub
`attempt` callables for controllable success/error outcomes per candidate --
same technique as test_streaming.py's _FaultInjectingAdapter, not the real
mock HTTP servers, so attempt counts and timing are exact and fast. Provider
names must be real ones (openai/anthropic/ollama) because
get_adapter_for_provider resolves a real adapter instance for each candidate
-- the scripted `attempt` callable below simply never calls it, so no network
call ever happens.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
import yaml

from gateway.config.loader import CircuitBreakerConfig, GatewayConfig
from gateway.providers.errors import NonRetryableProviderError, RetryableProviderError
from gateway.resilience import orchestrator
from gateway.resilience.circuit_breaker import (
    _failures_key,
    _opened_at_key,
    _probe_claimed_key,
    _state_key,
    check_breaker,
    record_failure,
)
from gateway.resilience.orchestrator import resolve_with_resilience
from gateway.schemas import ChatCompletionRequest, ChatMessage

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "test_config.yaml")

with open(_CONFIG_PATH) as f:
    _BASE_CONFIG = GatewayConfig.model_validate(yaml.safe_load(f))

_PROVIDERS = ("openai", "anthropic", "ollama")


def _config(*, failure_threshold=5, window_seconds=60, cooldown_seconds=30) -> GatewayConfig:
    return _BASE_CONFIG.model_copy(
        update={
            "circuit_breaker": CircuitBreakerConfig(
                failure_threshold=failure_threshold,
                window_seconds=window_seconds,
                cooldown_seconds=cooldown_seconds,
            )
        }
    )


def _request(model: str = "claude-sonnet") -> ChatCompletionRequest:
    return ChatCompletionRequest(model=model, messages=[ChatMessage(role="user", content="hi")])


async def _open_breaker(redis_client, config: GatewayConfig, provider: str = "anthropic") -> None:
    for _ in range(config.circuit_breaker.failure_threshold):
        await record_failure(redis_client, provider, was_probe=False, config=config.circuit_breaker)


def _spy(monkeypatch, name: str) -> list[tuple[tuple, dict]]:
    calls: list[tuple[tuple, dict]] = []
    original = getattr(orchestrator, name)

    async def wrapper(*args, **kwargs):
        calls.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(orchestrator, name, wrapper)
    return calls


class ScriptedAttempt:
    """Test-only `attempt` callable -- ignores the real adapter instance the
    orchestrator resolves and instead returns/raises a scripted outcome per
    provider, popped in order.
    """

    def __init__(self, scripts: dict[str, list[Exception | str]]):
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: dict[str, int] = {}

    async def __call__(self, provider, adapter, request):
        self.calls[provider] = self.calls.get(provider, 0) + 1
        queue = self._scripts.get(provider, [])
        outcome = queue.pop(0) if queue else f"result-from-{provider}"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest_asyncio.fixture(autouse=True)
async def _clean_breaker_state(redis_client, db_pool):
    yield
    for provider in _PROVIDERS:
        await redis_client.delete(
            _state_key(provider),
            _failures_key(provider),
            _opened_at_key(provider),
            _probe_claimed_key(provider),
        )
    await db_pool.execute("DELETE FROM circuit_breaker_history WHERE provider = ANY($1)", list(_PROVIDERS))


# --- primary success ---------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_succeeds_first_attempt_no_fallback_touched(redis_client, monkeypatch):
    config = _config()
    success_calls = _spy(monkeypatch, "record_success")
    scripted = ScriptedAttempt({"anthropic": ["ok"]})

    provider, model, result = await resolve_with_resilience(
        "anthropic", "claude-sonnet", _request(), config, redis_client, scripted
    )

    assert (provider, model, result) == ("anthropic", "claude-sonnet", "ok")
    assert scripted.calls == {"anthropic": 1}
    assert len(success_calls) == 1
    assert success_calls[0][0] == (redis_client, "anthropic", False)


# --- primary retry then fallback ---------------------------------------------


@pytest.mark.asyncio
async def test_primary_exhausts_three_attempts_then_falls_back(redis_client, monkeypatch):
    config = _config()
    failure_calls = _spy(monkeypatch, "record_failure")
    scripted = ScriptedAttempt(
        {
            "anthropic": [
                RetryableProviderError("t1"),
                RetryableProviderError("t2"),
                RetryableProviderError("t3"),
            ],
            "openai": ["fallback-ok"],
        }
    )

    provider, model, result = await resolve_with_resilience(
        "anthropic", "claude-sonnet", _request(), config, redis_client, scripted
    )

    assert provider == "openai"
    assert model == "gpt-4o-mini"
    assert result == "fallback-ok"
    assert scripted.calls["anthropic"] == 3
    assert scripted.calls["openai"] == 1
    assert len(failure_calls) == 1
    assert failure_calls[0][0] == (redis_client, "anthropic", False, config.circuit_breaker)


@pytest.mark.asyncio
async def test_primary_non_retryable_error_skips_retry_and_does_not_count_as_failure(
    redis_client, monkeypatch
):
    config = _config(failure_threshold=2)
    failure_calls = _spy(monkeypatch, "record_failure")
    scripted = ScriptedAttempt(
        {
            "anthropic": [NonRetryableProviderError("bad request")],
            "openai": ["fallback-ok"],
        }
    )

    provider, _model, _result = await resolve_with_resilience(
        "anthropic", "claude-sonnet", _request(), config, redis_client, scripted
    )

    assert provider == "openai"
    assert scripted.calls["anthropic"] == 1
    assert failure_calls == []

    # confirm the primary's failure count wasn't incremented by the
    # non-retryable error: one *real* failure still isn't enough to reach
    # threshold=2.
    await record_failure(redis_client, "anthropic", was_probe=False, config=config.circuit_breaker)
    decision = await check_breaker(redis_client, "anthropic", config.circuit_breaker)
    assert decision.allowed is True


# --- breaker already open -----------------------------------------------------


@pytest.mark.asyncio
async def test_primary_breaker_open_skips_straight_to_fallback(redis_client):
    config = _config(failure_threshold=2, cooldown_seconds=60)
    await _open_breaker(redis_client, config)

    scripted = ScriptedAttempt({"openai": ["fallback-ok"]})

    provider, _model, _result = await resolve_with_resilience(
        "anthropic", "claude-sonnet", _request(), config, redis_client, scripted
    )

    assert provider == "openai"
    assert scripted.calls.get("anthropic", 0) == 0
    assert scripted.calls["openai"] == 1


# --- fallback ordering ---------------------------------------------------------


@pytest.mark.asyncio
async def test_fallbacks_tried_in_order_one_attempt_each(redis_client):
    config = _config()
    scripted = ScriptedAttempt(
        {
            "anthropic": [
                RetryableProviderError("p1"),
                RetryableProviderError("p2"),
                RetryableProviderError("p3"),
            ],
            "openai": [RetryableProviderError("f1")],
            "ollama": ["fallback-ok"],
        }
    )

    provider, model, _result = await resolve_with_resilience(
        "anthropic", "claude-sonnet", _request(), config, redis_client, scripted
    )

    assert provider == "ollama"
    assert model == "llama3"
    assert scripted.calls["anthropic"] == 3
    assert scripted.calls["openai"] == 1
    assert scripted.calls["ollama"] == 1


@pytest.mark.asyncio
async def test_every_candidate_exhausted_raises_last_error(redis_client):
    config = _config()
    last_error = RetryableProviderError("ollama down")
    scripted = ScriptedAttempt(
        {
            "anthropic": [
                RetryableProviderError("a1"),
                RetryableProviderError("a2"),
                RetryableProviderError("a3"),
            ],
            "openai": [RetryableProviderError("o1")],
            "ollama": [last_error],
        }
    )

    with pytest.raises(RetryableProviderError) as excinfo:
        await resolve_with_resilience(
            "anthropic", "claude-sonnet", _request(), config, redis_client, scripted
        )

    assert excinfo.value is last_error


# --- half-open probes ----------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_gets_one_attempt_and_success_closes_breaker(redis_client, monkeypatch):
    config = _config(failure_threshold=2, cooldown_seconds=1)
    await _open_breaker(redis_client, config)
    await asyncio.sleep(1.2)

    success_calls = _spy(monkeypatch, "record_success")
    scripted = ScriptedAttempt({"anthropic": ["ok"]})

    provider, _model, _result = await resolve_with_resilience(
        "anthropic", "claude-sonnet", _request(), config, redis_client, scripted
    )

    assert provider == "anthropic"
    assert scripted.calls["anthropic"] == 1
    assert len(success_calls) == 1
    assert success_calls[0][0] == (redis_client, "anthropic", True)

    decision = await check_breaker(redis_client, "anthropic", config.circuit_breaker)
    assert decision.allowed is True
    assert decision.is_probe is False


@pytest.mark.asyncio
async def test_probe_failure_reopens_immediately_with_exactly_one_attempt(redis_client, monkeypatch):
    config = _config(failure_threshold=2, cooldown_seconds=1)
    await _open_breaker(redis_client, config)
    await asyncio.sleep(1.2)

    failure_calls = _spy(monkeypatch, "record_failure")
    scripted = ScriptedAttempt(
        {
            "anthropic": [RetryableProviderError("probe failed")],
            "openai": ["fallback-ok"],
        }
    )

    provider, _model, _result = await resolve_with_resilience(
        "anthropic", "claude-sonnet", _request(), config, redis_client, scripted
    )

    assert provider == "openai"
    assert scripted.calls["anthropic"] == 1
    assert len(failure_calls) == 1
    assert failure_calls[0][0] == (redis_client, "anthropic", True, config.circuit_breaker)

    decision = await check_breaker(redis_client, "anthropic", config.circuit_breaker)
    assert decision.allowed is False


@pytest.mark.asyncio
async def test_probe_non_retryable_error_calls_neither_record_success_nor_failure(
    redis_client, monkeypatch
):
    config = _config(failure_threshold=2, cooldown_seconds=1)
    await _open_breaker(redis_client, config)
    await asyncio.sleep(1.2)

    success_calls = _spy(monkeypatch, "record_success")
    failure_calls = _spy(monkeypatch, "record_failure")
    scripted = ScriptedAttempt(
        {
            "anthropic": [NonRetryableProviderError("bad request")],
            "openai": ["fallback-ok"],
        }
    )

    provider, _model, _result = await resolve_with_resilience(
        "anthropic", "claude-sonnet", _request(), config, redis_client, scripted
    )

    assert provider == "openai"
    assert scripted.calls["anthropic"] == 1
    assert all(call[0][1] != "anthropic" for call in success_calls)
    assert all(call[0][1] != "anthropic" for call in failure_calls)

    # probe slot still claimed -> still transitional, not forced closed or
    # reopened by the inconclusive probe outcome.
    decision = await check_breaker(redis_client, "anthropic", config.circuit_breaker)
    assert decision.allowed is False
