"""Admin API routes (TRD §6.2) -- operator-facing, behind a named admin
token (ADR-012), fully independent from team API keys. Every mutating
action writes an `audit_log` row recording who did what.
"""

from __future__ import annotations

import json
import secrets
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from gateway.auth.admin_auth import Admin, get_current_admin
from gateway.auth.team_auth import Team
from gateway.config.loader import get_config, reload_config
from gateway.db import get_pool
from gateway.ratelimit.budget import check_budget
from gateway.ratelimit.token_bucket import check_and_consume
from gateway.redis_client import get_redis

router = APIRouter(prefix="/admin")

_TEAM_BY_ID_QUERY = """
    SELECT id, name, allowed_models, rpm_limit, tpm_limit, daily_budget_usd, monthly_budget_usd, config
    FROM teams WHERE id = $1
"""


async def _fetch_team(team_id: str) -> Team | None:
    row = await get_pool().fetchrow(_TEAM_BY_ID_QUERY, team_id)
    if row is None:
        return None

    config = row["config"]
    if isinstance(config, str):
        config = json.loads(config)

    return Team(
        id=row["id"],
        name=row["name"],
        allowed_models=list(row["allowed_models"]),
        rpm_limit=row["rpm_limit"],
        tpm_limit=row["tpm_limit"],
        daily_budget_usd=row["daily_budget_usd"],
        monthly_budget_usd=row["monthly_budget_usd"],
        config=config,
    )


def _jsonable(row: dict) -> dict:
    """Decimal isn't JSON-serializable by default -- stringify it (audit_log
    before/after are jsonb columns that must be written as JSON text, since
    no jsonb codec is registered on the pool, per the existing convention in
    team_auth.py/conftest.py).
    """
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()}


# --- GET /teams/{team_id}/status ---------------------------------------------


@router.get("/teams/{team_id}/status")
async def get_team_status(team_id: str, admin: Admin = Depends(get_current_admin)) -> dict:
    team = await _fetch_team(team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="team not found")

    config = get_config()
    redis = get_redis()

    rate_limits = {}
    for tier, tier_config in config.priority_tiers.items():
        rpm_capacity = team.rpm_limit * tier_config.rpm_ceiling_pct / 100
        tpm_capacity = team.tpm_limit * tier_config.rpm_ceiling_pct / 100

        rpm_result = await check_and_consume(
            redis,
            f"ratelimit:{team_id}:{tier}:rpm",
            capacity=rpm_capacity,
            refill_per_second=rpm_capacity / 60,
            cost=0,
        )
        tpm_result = await check_and_consume(
            redis,
            f"ratelimit:{team_id}:{tier}:tpm",
            capacity=tpm_capacity,
            refill_per_second=tpm_capacity / 60,
            cost=0,
        )

        rate_limits[tier] = {
            "rpm_capacity": rpm_capacity,
            "rpm_remaining": rpm_result.remaining,
            "tpm_capacity": tpm_capacity,
            "tpm_remaining": tpm_result.remaining,
        }

    budget_status = await check_budget(team)

    return {
        "team_id": team.id,
        "rate_limits": rate_limits,
        "budget": budget_status.model_dump(),
    }


# --- PATCH /teams/{team_id}/limits -------------------------------------------


class UpdateTeamLimitsRequest(BaseModel):
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    daily_budget_usd: Decimal | None = None
    monthly_budget_usd: Decimal | None = None


@router.patch("/teams/{team_id}/limits")
async def update_team_limits(
    team_id: str, body: UpdateTeamLimitsRequest, admin: Admin = Depends(get_current_admin)
) -> dict:
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")

    fields = list(updates.keys())
    pool = get_pool()

    before_row = await pool.fetchrow(f"SELECT {', '.join(fields)} FROM teams WHERE id = $1", team_id)
    if before_row is None:
        raise HTTPException(status_code=404, detail="team not found")
    before = _jsonable(dict(before_row))

    set_clause = ", ".join(f"{field} = ${i + 2}" for i, field in enumerate(fields))
    after_row = await pool.fetchrow(
        f"UPDATE teams SET {set_clause}, updated_at = now() WHERE id = $1 RETURNING {', '.join(fields)}",
        team_id,
        *(updates[field] for field in fields),
    )
    after = _jsonable(dict(after_row))

    await pool.execute(
        "INSERT INTO audit_log (admin_name, action, team_id, before, after) VALUES ($1, $2, $3, $4, $5)",
        admin.name,
        "update_team_limits",
        team_id,
        json.dumps(before),
        json.dumps(after),
    )

    return after


# --- GET /teams/{team_id}/spend ----------------------------------------------

_SPEND_WINDOW = "30 days"


@router.get("/teams/{team_id}/spend")
async def get_team_spend(team_id: str, admin: Admin = Depends(get_current_admin)) -> dict:
    team = await _fetch_team(team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="team not found")

    pool = get_pool()
    by_day = await pool.fetch(
        f"""
        SELECT date_trunc('day', created_at) AS day,
               sum(cost_usd) AS cost_usd,
               sum(input_tokens) AS input_tokens,
               sum(output_tokens) AS output_tokens
        FROM spend_ledger
        WHERE team_id = $1 AND created_at >= now() - interval '{_SPEND_WINDOW}'
        GROUP BY day
        ORDER BY day DESC
        """,
        team_id,
    )
    by_provider_model = await pool.fetch(
        f"""
        SELECT provider, model,
               sum(cost_usd) AS cost_usd,
               sum(input_tokens) AS input_tokens,
               sum(output_tokens) AS output_tokens,
               count(*) AS request_count
        FROM spend_ledger
        WHERE team_id = $1 AND created_at >= now() - interval '{_SPEND_WINDOW}'
        GROUP BY provider, model
        """,
        team_id,
    )

    return {
        "team_id": team_id,
        "by_day": [dict(row) for row in by_day],
        "by_provider_model": [dict(row) for row in by_provider_model],
    }


# --- POST /teams --------------------------------------------------------------


class CreateTeamRequest(BaseModel):
    name: str
    allowed_models: list[str]
    rpm_limit: int
    tpm_limit: int
    daily_budget_usd: Decimal | None = None
    monthly_budget_usd: Decimal | None = None


@router.post("/teams")
async def create_team(body: CreateTeamRequest, admin: Admin = Depends(get_current_admin)) -> dict:
    team_id = f"team-{uuid.uuid4().hex[:12]}"
    api_key = secrets.token_urlsafe(32)
    pool = get_pool()

    await pool.execute(
        """
        INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit, daily_budget_usd, monthly_budget_usd)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        team_id,
        body.name,
        body.allowed_models,
        body.rpm_limit,
        body.tpm_limit,
        body.daily_budget_usd,
        body.monthly_budget_usd,
    )
    # ADR-012: opaque, random, not self-encoding -- this is the one place a
    # team key is minted; no general rotation/reissuance endpoint exists.
    await pool.execute("INSERT INTO team_api_keys (token, team_id) VALUES ($1, $2)", api_key, team_id)

    await pool.execute(
        "INSERT INTO audit_log (admin_name, action, team_id, before, after) VALUES ($1, $2, $3, $4, $5)",
        admin.name,
        "create_team",
        team_id,
        None,
        json.dumps(_jsonable(body.model_dump())),
    )

    return {"team_id": team_id, "api_key": api_key}


# --- GET /audit-log ------------------------------------------------------------

_AUDIT_LOG_LIMIT = 100


@router.get("/audit-log")
async def list_audit_log(team_id: str | None = None, admin: Admin = Depends(get_current_admin)) -> dict:
    pool = get_pool()
    if team_id is not None:
        rows = await pool.fetch(
            """
            SELECT id, admin_name, action, team_id, before, after, created_at
            FROM audit_log WHERE team_id = $1
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            team_id,
            _AUDIT_LOG_LIMIT,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, admin_name, action, team_id, before, after, created_at
            FROM audit_log
            ORDER BY created_at DESC, id DESC
            LIMIT $1
            """,
            _AUDIT_LOG_LIMIT,
        )

    entries = []
    for row in rows:
        entry = dict(row)
        for field in ("before", "after"):
            if isinstance(entry[field], str):
                entry[field] = json.loads(entry[field])
        entries.append(entry)

    return {"entries": entries}


# --- POST /config/reload -------------------------------------------------------


@router.post("/config/reload")
async def reload_config_endpoint(admin: Admin = Depends(get_current_admin)) -> dict:
    try:
        reload_config()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "reloaded"}
