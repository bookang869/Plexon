"""Generic retry+fallback+breaker orchestration (TRD §3 steps 6-7, PRD Core
Feature 3, CLAUDE.md's CRITICAL retry rule). `resolve_with_resilience` is
parameterized over "how to make one attempt" so step 2's streaming path can
reuse it for a "fetch the first chunk" operation instead of a full
`chat_completion` call -- `call_with_resilience` is where the
non-streaming-specific typing lives.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from redis.asyncio import Redis
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gateway.config.loader import GatewayConfig
from gateway.providers.base import ProviderAdapter
from gateway.providers.errors import (
    NonRetryableProviderError,
    ProviderError,
    RetryableProviderError,
)
from gateway.providers.registry import get_adapter_for_provider
from gateway.resilience.circuit_breaker import check_breaker, record_failure, record_success
from gateway.resilience.fallback import resolve_fallback_chain
from gateway.schemas import ChatCompletionRequest, ChatCompletionResponse

T = TypeVar("T")

_RETRY_WAIT_MULTIPLIER_SECONDS = 0.1
_RETRY_WAIT_MAX_SECONDS = 2.0
_PRIMARY_MAX_ATTEMPTS = 3


async def _attempt_with_retry(
    provider: str,
    adapter: ProviderAdapter,
    request: ChatCompletionRequest,
    attempt: Callable[[str, ProviderAdapter, ChatCompletionRequest], Awaitable[T]],
) -> T:
    @retry(
        stop=stop_after_attempt(_PRIMARY_MAX_ATTEMPTS),
        wait=wait_exponential(multiplier=_RETRY_WAIT_MULTIPLIER_SECONDS, max=_RETRY_WAIT_MAX_SECONDS),
        retry=retry_if_exception_type(RetryableProviderError),
        reraise=True,
    )
    async def _call() -> T:
        return await attempt(provider, adapter, request)

    return await _call()


async def resolve_with_resilience(
    primary_provider: str,
    primary_model: str,
    request: ChatCompletionRequest,
    config: GatewayConfig,
    redis: Redis,
    attempt: Callable[[str, ProviderAdapter, ChatCompletionRequest], Awaitable[T]],
) -> tuple[str, str, T]:
    """Walks primary-then-fallback-chain, returning (serving_provider,
    serving_model, attempt_result) from whichever candidate succeeded.
    `attempt` performs exactly one call attempt and must raise
    RetryableProviderError/NonRetryableProviderError on failure -- it does
    not retry internally; retry policy lives entirely here. Raises the last
    ProviderError encountered if every candidate is exhausted or skipped.
    """
    candidates: list[tuple[str, str, ChatCompletionRequest]] = [
        (primary_provider, primary_model, request)
    ]
    for fallback_provider, fallback_model in resolve_fallback_chain(
        primary_provider, primary_model, config
    ):
        candidates.append(
            (fallback_provider, fallback_model, request.model_copy(update={"model": fallback_model}))
        )

    last_error: ProviderError | None = None

    for index, (provider, model, candidate_request) in enumerate(candidates):
        is_primary = index == 0

        decision = await check_breaker(redis, provider, config.circuit_breaker)
        if not decision.allowed:
            # Zero attempts against this candidate -- a normal, expected
            # outcome (breaker is open), not a failure to log.
            last_error = RetryableProviderError(f"circuit breaker open for provider {provider!r}")
            continue

        adapter = get_adapter_for_provider(provider, config)

        try:
            if is_primary and not decision.is_probe:
                result = await _attempt_with_retry(provider, adapter, candidate_request, attempt)
            else:
                # A half-open probe (primary or fallback) is a single
                # cautious canary request, and every fallback candidate gets
                # exactly one attempt -- retrying either multiplies
                # worst-case latency per hop and works against the breaker's
                # purpose of backing off faster during a real outage.
                result = await attempt(provider, adapter, candidate_request)
        except RetryableProviderError as exc:
            last_error = exc
            await record_failure(redis, provider, decision.is_probe, config.circuit_breaker)
            continue
        except NonRetryableProviderError as exc:
            # A rejected request says nothing about provider health -- don't
            # record a failure. If this was the probe, its outcome is
            # inconclusive too, so leave probe_claimed to expire naturally
            # rather than forcing a close/reopen decision.
            last_error = exc
            continue

        await record_success(redis, provider, decision.is_probe)
        return provider, model, result

    assert last_error is not None
    raise last_error


async def call_with_resilience(
    request: ChatCompletionRequest,
    provider: str,
    model: str,
    adapter: ProviderAdapter,
    config: GatewayConfig,
    redis: Redis,
) -> tuple[str, ChatCompletionResponse]:
    """Non-streaming convenience wrapper. Returns (serving_provider,
    response) -- the serving provider may differ from `provider` once a
    fallback has served the request.
    """

    async def _attempt(
        candidate_provider: str, candidate_adapter: ProviderAdapter, candidate_request: ChatCompletionRequest
    ) -> ChatCompletionResponse:
        return await candidate_adapter.chat_completion(candidate_request)

    serving_provider, _serving_model, response = await resolve_with_resilience(
        provider, model, request, config, redis, _attempt
    )
    return serving_provider, response
