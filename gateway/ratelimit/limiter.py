"""Tiered rate limiting (ADR-011, ADR-020) built on top of the generic
token-bucket primitive (`gateway/ratelimit/token_bucket.py`). Each team/tier
pair gets its own rpm and tpm bucket in Redis, keyed per TRD §4.2
(`ratelimit:{team_id}:{tier}:rpm` / `:tpm`); both buckets are sized from the
tier's `rpm_ceiling_pct` against the team's own `rpm_limit`/`tpm_limit`
(there is deliberately no separate `tpm_ceiling_pct` -- the one percentage
covers both dimensions).
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from gateway.auth.team_auth import Team
from gateway.config.loader import GatewayConfig, get_config
from gateway.ratelimit.token_bucket import check_and_consume, refund
from gateway.redis_client import get_redis
from gateway.schemas import ChatCompletionRequest

# Completion-length assumption when a request doesn't set max_tokens, mirrors
# AnthropicAdapter's own fallback (gateway/providers/anthropic_adapter.py)
# for what an unbounded completion request plausibly costs.
_DEFAULT_MAX_TOKENS_ESTIMATE = 1024


def resolve_tier(x_priority: str | None, config: GatewayConfig) -> str:
    """Defaults to 'realtime' when the header is absent. Raises
    HTTPException(400) for a tier name not present in config.priority_tiers
    -- don't silently fall back to 'realtime' for a typo'd/unknown tier,
    that would mask a caller bug.
    """
    tier = x_priority or "realtime"
    if tier not in config.priority_tiers:
        raise HTTPException(status_code=400, detail=f"unknown priority tier: {tier!r}")
    return tier


def estimate_tokens(request: ChatCompletionRequest) -> int:
    """Pre-call best-effort estimate of total tokens this request will
    consume (prompt + completion), used only to reserve tpm-bucket capacity
    before the real usage is known.

    Heuristic: prompt tokens are approximated by whitespace word count
    across all messages (the same fabrication convention the mocks and
    `gateway/streaming.py`'s tee-assembly already use for fake `usage`
    objects); completion tokens are `request.max_tokens` when the caller set
    one, else `_DEFAULT_MAX_TOKENS_ESTIMATE`. This is a genuine
    approximation -- it exists to size a reservation, not to bill accurately.
    """
    prompt_text = " ".join(m.content for m in request.messages)
    prompt_tokens = max(1, len(prompt_text.split()))
    completion_tokens = (
        request.max_tokens if request.max_tokens is not None else _DEFAULT_MAX_TOKENS_ESTIMATE
    )
    return prompt_tokens + completion_tokens


class RateLimitDecision(BaseModel):
    allowed: bool
    retry_after_seconds: float | None
    tier: str
    estimated_tokens: int


def _bucket_capacity(limit: int, ceiling_pct: int) -> float:
    return limit * ceiling_pct / 100


async def check_rate_limit(team: Team, tier: str, estimated_tokens: int) -> RateLimitDecision:
    """Checks the tier's rpm bucket (cost=1) at key
    `ratelimit:{team.id}:{tier}:rpm`, then the tpm bucket (cost=
    estimated_tokens) at `ratelimit:{team.id}:{tier}:tpm`. Both must have
    room for the request to be admitted.

    Rollback on partial denial: if the rpm check passes but the tpm check
    fails, the rpm deduction is refunded before returning `allowed=False` --
    otherwise a denied request would permanently consume a unit of rpm
    capacity. A brief window exists where two concurrent requests could each
    pass rpm and then race on tpm in a way a single merged two-key Lua
    script would avoid -- an accepted tradeoff for this project's scope
    (ADR-002), not worth a bespoke dual-key script.
    """
    config = get_config()
    ceiling_pct = config.priority_tiers[tier].rpm_ceiling_pct

    rpm_capacity = _bucket_capacity(team.rpm_limit, ceiling_pct)
    tpm_capacity = _bucket_capacity(team.tpm_limit, ceiling_pct)
    rpm_key = f"ratelimit:{team.id}:{tier}:rpm"
    tpm_key = f"ratelimit:{team.id}:{tier}:tpm"

    redis = get_redis()

    rpm_result = await check_and_consume(
        redis, rpm_key, capacity=rpm_capacity, refill_per_second=rpm_capacity / 60, cost=1.0
    )
    if not rpm_result.allowed:
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=rpm_result.retry_after_seconds,
            tier=tier,
            estimated_tokens=estimated_tokens,
        )

    tpm_result = await check_and_consume(
        redis,
        tpm_key,
        capacity=tpm_capacity,
        refill_per_second=tpm_capacity / 60,
        cost=float(estimated_tokens),
    )
    if not tpm_result.allowed:
        await refund(redis, rpm_key, capacity=rpm_capacity, amount=1.0)
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=tpm_result.retry_after_seconds,
            tier=tier,
            estimated_tokens=estimated_tokens,
        )

    return RateLimitDecision(
        allowed=True, retry_after_seconds=None, tier=tier, estimated_tokens=estimated_tokens
    )


async def reconcile_tpm(team: Team, tier: str, estimated_tokens: int, actual_tokens: int) -> None:
    """Called after the real response is known. If actual < estimated,
    refunds the difference (the tpm bucket was over-charged at admission
    time). If actual > estimated, the shortfall is silently absorbed -- a
    response has already been delivered, there's no way to retroactively
    deny it, so the bucket just runs slightly tighter until the next
    natural refill.
    """
    if actual_tokens >= estimated_tokens:
        return

    config = get_config()
    ceiling_pct = config.priority_tiers[tier].rpm_ceiling_pct
    tpm_capacity = _bucket_capacity(team.tpm_limit, ceiling_pct)
    tpm_key = f"ratelimit:{team.id}:{tier}:tpm"

    await refund(get_redis(), tpm_key, capacity=tpm_capacity, amount=estimated_tokens - actual_tokens)
