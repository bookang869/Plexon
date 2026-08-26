"""Tests for gateway/auth/team_auth.py. Requires real Postgres (ADR-006 only
exempts provider mocks from needing real backing services): `docker compose
-f deploy/docker-compose.yml up -d postgres`.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from gateway.auth.team_auth import Team, get_current_team


@pytest.mark.asyncio
async def test_valid_key_returns_team_with_correct_fields(seeded_team):
    team = await get_current_team(authorization=f"Bearer {seeded_team['api_key']}")

    assert isinstance(team, Team)
    assert team.id == seeded_team["team_id"]
    assert team.name == "Test Team"
    assert team.allowed_models == ["gpt-4o-mini", "claude-sonnet"]
    assert team.rpm_limit == 60
    assert team.tpm_limit == 10000
    assert team.daily_budget_usd == pytest.approx(10.00)
    assert team.monthly_budget_usd == pytest.approx(200.00)
    assert team.config == {}


@pytest.mark.asyncio
async def test_valid_key_without_bearer_prefix_also_works(seeded_team, db_pool):
    team = await get_current_team(authorization=seeded_team["api_key"])
    assert team.id == seeded_team["team_id"]


@pytest.mark.asyncio
async def test_missing_authorization_header_returns_401(db_pool):
    with pytest.raises(HTTPException) as exc_info:
        await get_current_team(authorization=None)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_unknown_key_returns_401(db_pool):
    with pytest.raises(HTTPException) as exc_info:
        await get_current_team(authorization="Bearer nonexistent-key")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_revoked_key_returns_401(revoked_team):
    with pytest.raises(HTTPException) as exc_info:
        await get_current_team(authorization=f"Bearer {revoked_team['api_key']}")
    assert exc_info.value.status_code == 401
