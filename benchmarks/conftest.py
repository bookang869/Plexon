"""Shared fixtures for phases/benchmarks. Mirrors tests/conftest.py's
db_pool/_insert_team/_delete_team shape, but points at the live
docker-compose Postgres the benchmark suite runs against on the host
(same posture as tests/, which also talks to the published 5433 port).
"""

from __future__ import annotations

import os
import uuid

import pytest_asyncio

os.environ.setdefault("PLEXON_DATABASE_URL", "postgresql://plexon:plexon@localhost:5433/plexon")
os.environ.setdefault("PLEXON_REDIS_URL", "redis://localhost:6379/0")

from gateway.db import close_pool, get_pool, init_pool
from gateway.redis_client import close_redis, get_redis, init_redis


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
async def benchmark_team(db_pool):
    """A team with generous rpm/tpm limits and a large budget, so
    throughput/overhead/failover benchmarks aren't incidentally rate-limited
    or budget-blocked by the very thing they're trying to measure. Includes
    claude-sonnet--fault-error (ADR-025's magic model-name fault trigger) so
    the failover benchmark can use this same shared fixture.
    """
    team_id = f"team-benchmark-{uuid.uuid4().hex[:8]}"
    api_key = f"benchmark-key-{uuid.uuid4().hex}"

    await db_pool.execute(
        """
        INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit,
                            daily_budget_usd, monthly_budget_usd, config)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        team_id,
        "Benchmark Team",
        ["gpt-4o-mini", "claude-sonnet", "claude-sonnet--fault-error"],
        1_000_000,
        100_000_000,
        "100000.00",
        "1000000.00",
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
