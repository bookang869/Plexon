"""Shared pytest fixtures. Tests that need real Postgres (ADR-006 exempts
only the provider mocks from this -- team auth genuinely needs the database)
rely on `docker compose -f deploy/docker-compose.yml up -d postgres` already
running locally.
"""

from __future__ import annotations

import os
import uuid

import pytest_asyncio

os.environ.setdefault("PLEXON_DATABASE_URL", "postgresql://plexon:plexon@localhost:5432/plexon")

from gateway.db import close_pool, get_pool, init_pool


@pytest_asyncio.fixture
async def db_pool():
    await init_pool()
    yield get_pool()
    await close_pool()


@pytest_asyncio.fixture
async def seeded_team(db_pool):
    """Inserts a test team + a live team API key directly via the pool
    (there's no admin API yet to do this through), tears both down after.
    """
    team_id = f"team-test-{uuid.uuid4().hex[:8]}"
    api_key = f"test-key-{uuid.uuid4().hex}"

    await db_pool.execute(
        """
        INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit,
                            daily_budget_usd, monthly_budget_usd, config)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        team_id,
        "Test Team",
        ["gpt-4o-mini", "claude-sonnet"],
        60,
        10000,
        "10.00",
        "200.00",
        "{}",
    )
    await db_pool.execute(
        "INSERT INTO team_api_keys (token, team_id) VALUES ($1, $2)",
        api_key,
        team_id,
    )

    yield {"team_id": team_id, "api_key": api_key}

    await db_pool.execute("DELETE FROM team_api_keys WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM teams WHERE id = $1", team_id)


@pytest_asyncio.fixture
async def revoked_team(db_pool):
    """A team whose only API key is already revoked."""
    team_id = f"team-test-{uuid.uuid4().hex[:8]}"
    api_key = f"test-key-{uuid.uuid4().hex}"

    await db_pool.execute(
        """
        INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit,
                            daily_budget_usd, monthly_budget_usd, config)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        team_id,
        "Revoked Test Team",
        ["gpt-4o-mini"],
        60,
        10000,
        None,
        None,
        "{}",
    )
    await db_pool.execute(
        "INSERT INTO team_api_keys (token, team_id, revoked_at) VALUES ($1, $2, now())",
        api_key,
        team_id,
    )

    yield {"team_id": team_id, "api_key": api_key}

    await db_pool.execute("DELETE FROM team_api_keys WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM teams WHERE id = $1", team_id)
