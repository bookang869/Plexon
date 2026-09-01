"""Team API key authentication (ADR-012: team keys and admin tokens are two
independent opaque-token systems -- this module only implements the former).
Every lookup hits Postgres directly; no in-process cache, so a revoked key
stops working on the very next request (ADR-007).
"""

from __future__ import annotations

import json
from decimal import Decimal

from fastapi import Header, HTTPException
from pydantic import BaseModel

from gateway.db import get_pool
from gateway.observability import tracing

_INVALID_KEY_DETAIL = "invalid or missing API key"

_TEAM_LOOKUP_QUERY = """
    SELECT teams.id, teams.name, teams.allowed_models, teams.rpm_limit,
           teams.tpm_limit, teams.daily_budget_usd, teams.monthly_budget_usd,
           teams.config
    FROM team_api_keys
    JOIN teams ON teams.id = team_api_keys.team_id
    WHERE team_api_keys.token = $1 AND team_api_keys.revoked_at IS NULL
"""


class Team(BaseModel):
    id: str
    name: str
    allowed_models: list[str]
    rpm_limit: int
    tpm_limit: int
    daily_budget_usd: Decimal | None
    monthly_budget_usd: Decimal | None
    config: dict  # teams.config jsonb, raw -- interpreted by the enrichment step


def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ")
    return authorization


async def get_current_team(authorization: str | None = Header(default=None)) -> Team:
    with tracing.get_tracer().start_as_current_span("auth") as span:
        token = _extract_token(authorization)
        if not token:
            raise HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)

        row = await get_pool().fetchrow(_TEAM_LOOKUP_QUERY, token)
        if row is None:
            raise HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)

        config = row["config"]
        if isinstance(config, str):
            config = json.loads(config)

        team = Team(
            id=row["id"],
            name=row["name"],
            allowed_models=list(row["allowed_models"]),
            rpm_limit=row["rpm_limit"],
            tpm_limit=row["tpm_limit"],
            daily_budget_usd=row["daily_budget_usd"],
            monthly_budget_usd=row["monthly_budget_usd"],
            config=config,
        )
        span.set_attribute("team_id", team.id)
        return team
