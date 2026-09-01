"""Shared pytest fixtures. Tests that need real Postgres (ADR-006 exempts
only the provider mocks from this -- team auth genuinely needs the database)
rely on `docker compose -f deploy/docker-compose.yml up -d postgres` already
running locally.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

os.environ.setdefault("PLEXON_DATABASE_URL", "postgresql://plexon:plexon@localhost:5433/plexon")
os.environ.setdefault("PLEXON_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault(
    "PLEXON_CONFIG_PATH", os.path.join(os.path.dirname(__file__), "fixtures", "test_config.yaml")
)

from gateway.db import close_pool, get_pool, init_pool
from gateway.providers import registry as provider_registry
from gateway.redis_client import close_redis, get_redis, init_redis


@pytest.fixture(autouse=True)
def _reset_provider_registry_cache():
    """gateway/providers/registry.py caches one adapter (and its httpx
    connection pool) per provider for the life of the process -- correct for
    the single-instance/single-event-loop production design (ADR-007), but
    pytest-asyncio gives each test function its own event loop. Without this,
    a cached adapter's httpx.AsyncClient created in one test's loop gets
    reused (and its stale pooled connection closed) inside a later test's
    already-closed loop, blowing up with "Event loop is closed" -- most
    visibly on streaming requests, which hold a connection open longer.
    """
    yield
    provider_registry._adapters.clear()


@pytest_asyncio.fixture
async def db_pool():
    await init_pool()
    yield get_pool()
    await close_pool()


@pytest_asyncio.fixture
async def redis_client():
    await init_redis()
    yield get_redis()
    await close_redis()


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

    await db_pool.execute("DELETE FROM spend_ledger WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM alert_history WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM team_api_keys WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM teams WHERE id = $1", team_id)


@pytest_asyncio.fixture
async def admin_token(db_pool):
    """Inserts a live admin token directly via the pool (there's no admin
    endpoint to create one through), tears it down after.
    """
    token = f"admin-test-{uuid.uuid4().hex}"
    admin_name = "test-admin"

    await db_pool.execute(
        "INSERT INTO admin_tokens (token, admin_name) VALUES ($1, $2)", token, admin_name
    )

    yield {"token": token, "admin_name": admin_name}

    await db_pool.execute("DELETE FROM admin_tokens WHERE token = $1", token)


@pytest_asyncio.fixture
async def revoked_admin_token(db_pool):
    """An admin token that's already revoked."""
    token = f"admin-test-{uuid.uuid4().hex}"

    await db_pool.execute(
        "INSERT INTO admin_tokens (token, admin_name, revoked_at) VALUES ($1, $2, now())",
        token,
        "revoked-admin",
    )

    yield {"token": token}

    await db_pool.execute("DELETE FROM admin_tokens WHERE token = $1", token)


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
