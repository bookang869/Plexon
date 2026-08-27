"""Per-team dollar-budget tracking and enforcement (ADR-004, ADR-018). Unlike
`token_bucket.py`'s refilling bucket, spend is a monotonically-increasing
running counter within a period compared against a ceiling -- a different
enough problem that it gets its own primitive rather than being forced into
the bucket abstraction.

Dual-write per ADR-004: Redis holds a fast-path daily/monthly counter
(`spend:{team_id}:{daily,monthly}:{period}`, TRD §4.2), while `spend_ledger`
in Postgres is the durable source of truth -- the Redis counters are
explicitly disposable and could be rebuilt from the ledger after a restart.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from gateway.auth.team_auth import Team
from gateway.config.loader import PricingConfig
from gateway.db import get_pool
from gateway.redis_client import get_redis
from gateway.schemas import Usage

logger = logging.getLogger(__name__)

_WARNING_THRESHOLD = 0.8

# TTLs extend past each period's natural end so a counter never expires
# mid-period; the ledger, not Redis, is where historical spend actually lives.
_DAILY_TTL_SECONDS = 2 * 24 * 60 * 60
_MONTHLY_TTL_SECONDS = 32 * 24 * 60 * 60

_INSERT_LEDGER_ROW = """
    INSERT INTO spend_ledger (team_id, provider, model, input_tokens, output_tokens, cost_usd, request_id)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
"""


def compute_cost(usage: Usage, provider: str, model: str, pricing: PricingConfig) -> Decimal:
    provider_pricing = getattr(pricing, provider, None)
    if provider_pricing is None or model not in provider_pricing:
        raise ValueError(f"no pricing configured for provider={provider!r} model={model!r}")

    model_pricing = provider_pricing[model]
    input_cost = (Decimal(usage.prompt_tokens) / Decimal(1000)) * Decimal(str(model_pricing.input_per_1k))
    output_cost = (Decimal(usage.completion_tokens) / Decimal(1000)) * Decimal(
        str(model_pricing.output_per_1k)
    )
    return input_cost + output_cost


def _daily_key(team_id: str) -> str:
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"spend:{team_id}:daily:{date}"


def _monthly_key(team_id: str) -> str:
    month = datetime.now(UTC).strftime("%Y-%m")
    return f"spend:{team_id}:monthly:{month}"


def _utilization(spend: Decimal, budget: Decimal) -> float:
    if budget == 0:
        return float("inf") if spend > 0 else 0.0
    return float(spend / budget)


class BudgetStatus(BaseModel):
    blocked: bool
    warning: bool
    daily_utilization: float | None
    monthly_utilization: float | None


async def check_budget(team: Team) -> BudgetStatus:
    redis = get_redis()
    daily_utilization: float | None = None
    monthly_utilization: float | None = None

    if team.daily_budget_usd is not None:
        raw = await redis.get(_daily_key(team.id))
        spend = Decimal(raw.decode()) if raw is not None else Decimal(0)
        daily_utilization = _utilization(spend, team.daily_budget_usd)

    if team.monthly_budget_usd is not None:
        raw = await redis.get(_monthly_key(team.id))
        spend = Decimal(raw.decode()) if raw is not None else Decimal(0)
        monthly_utilization = _utilization(spend, team.monthly_budget_usd)

    blocked = (daily_utilization is not None and daily_utilization >= 1.0) or (
        monthly_utilization is not None and monthly_utilization >= 1.0
    )
    warning = not blocked and (
        (daily_utilization is not None and daily_utilization >= _WARNING_THRESHOLD)
        or (monthly_utilization is not None and monthly_utilization >= _WARNING_THRESHOLD)
    )

    return BudgetStatus(
        blocked=blocked,
        warning=warning,
        daily_utilization=daily_utilization,
        monthly_utilization=monthly_utilization,
    )


async def record_spend(
    team: Team, provider: str, model: str, usage: Usage, cost: Decimal, request_id: str
) -> None:
    redis = get_redis()
    daily_key = _daily_key(team.id)
    monthly_key = _monthly_key(team.id)
    cost_float = float(cost)

    await redis.incrbyfloat(daily_key, cost_float)
    await redis.expire(daily_key, _DAILY_TTL_SECONDS)
    await redis.incrbyfloat(monthly_key, cost_float)
    await redis.expire(monthly_key, _MONTHLY_TTL_SECONDS)

    try:
        await get_pool().execute(
            _INSERT_LEDGER_ROW,
            team.id,
            provider,
            model,
            usage.prompt_tokens,
            usage.completion_tokens,
            cost,
            request_id,
        )
    except Exception:
        # The provider already answered the caller -- a durable-ledger write
        # failure is a real gap worth surfacing loudly, but not one that
        # should turn a successful response into a 500 (ADR-002 scope: no
        # retry/queue infrastructure around this).
        logger.exception(
            "failed to write spend_ledger row for team=%s request_id=%s", team.id, request_id
        )
