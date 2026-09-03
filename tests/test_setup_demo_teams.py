"""Tests for scripts/setup_demo_teams.py -- runs against the real app's
Postgres pool (db_pool fixture), same convention as tests/test_admin.py.
Verifies the idempotency and roster shape the AC and step spec require.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.setup_demo_teams import DEMO_TEAMS, seed_demo_teams

_DEPENDENT_TABLES = ("team_api_keys", "spend_ledger", "audit_log", "alert_history")


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_demo_teams(db_pool):
    yield
    for entry in DEMO_TEAMS:
        team_id = entry["team_id"]
        for table in _DEPENDENT_TABLES:
            await db_pool.execute(f"DELETE FROM {table} WHERE team_id = $1", team_id)
        await db_pool.execute("DELETE FROM teams WHERE id = $1", team_id)


@pytest.mark.asyncio
async def test_seed_demo_teams_creates_four_teams_with_api_keys(db_pool):
    seeded = await seed_demo_teams()

    assert len(seeded) == 4
    assert len(DEMO_TEAMS) == 4

    for team in seeded:
        assert team["team_id"].startswith("demo-")
        assert team["api_key"]

        row = await db_pool.fetchrow(
            "SELECT id, rpm_limit, tpm_limit FROM teams WHERE id = $1", team["team_id"]
        )
        assert row is not None
        assert row["rpm_limit"] == team["rpm_limit"]
        assert row["tpm_limit"] == team["tpm_limit"]

        key_row = await db_pool.fetchrow(
            "SELECT team_id FROM team_api_keys WHERE token = $1", team["api_key"]
        )
        assert key_row is not None
        assert key_row["team_id"] == team["team_id"]


@pytest.mark.asyncio
async def test_seed_demo_teams_is_idempotent(db_pool):
    first = await seed_demo_teams()
    second = await seed_demo_teams()

    first_ids = sorted(t["team_id"] for t in first)
    second_ids = sorted(t["team_id"] for t in second)
    assert first_ids == second_ids

    # re-running rotates the api key rather than leaving stale duplicate rows
    for team in second:
        count = await db_pool.fetchval("SELECT count(*) FROM teams WHERE id = $1", team["team_id"])
        assert count == 1
        key_count = await db_pool.fetchval(
            "SELECT count(*) FROM team_api_keys WHERE team_id = $1", team["team_id"]
        )
        assert key_count == 1


@pytest.mark.asyncio
async def test_seed_demo_teams_have_varied_budgets(db_pool):
    seeded = await seed_demo_teams()
    daily_budgets = {Decimal(t["daily_budget_usd"]) for t in seeded}
    assert len(daily_budgets) > 1
