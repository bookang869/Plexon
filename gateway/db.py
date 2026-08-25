"""Postgres connection pool (ADR-004: durable state -- team config, spend
ledger, audit log, circuit-breaker/health history).
"""

from __future__ import annotations

import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    dsn = os.environ["PLEXON_DATABASE_URL"]
    _pool = await asyncpg.create_pool(dsn)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db pool not initialized; call init_pool() first")
    return _pool
