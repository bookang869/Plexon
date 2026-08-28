"""Tests for gateway/auth/admin_auth.py + gateway/admin/routes.py (TRD §6.2,
ADR-012). Runs against the real app (in-process), real Redis/Postgres --
`docker compose -f deploy/docker-compose.yml up -d redis postgres`.
"""

from __future__ import annotations

import json
import os
import pathlib

import httpx
import pytest
import pytest_asyncio

from gateway.auth.team_auth import get_current_team
from gateway.config.loader import get_config, reload_config, start_config_watcher
from gateway.main import app
from gateway.ratelimit.budget import _daily_key
from gateway.ratelimit.token_bucket import check_and_consume

_config_loaded = False


@pytest_asyncio.fixture
async def client(db_pool, redis_client):
    global _config_loaded
    if not _config_loaded:
        start_config_watcher()
        _config_loaded = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as ac:
        yield ac


def _admin_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- admin auth: 401 across all six endpoints --------------------------------

_ADMIN_ROUTES = [
    ("GET", "/admin/teams/some-team/status", None),
    ("PATCH", "/admin/teams/some-team/limits", {"rpm_limit": 10}),
    ("GET", "/admin/teams/some-team/spend", None),
    (
        "POST",
        "/admin/teams",
        {"name": "x", "allowed_models": ["gpt-4o-mini"], "rpm_limit": 10, "tpm_limit": 100},
    ),
    ("GET", "/admin/audit-log", None),
    ("POST", "/admin/config/reload", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", _ADMIN_ROUTES)
async def test_missing_admin_token_returns_401(client, method, path, body):
    resp = await client.request(method, path, json=body)
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", _ADMIN_ROUTES)
async def test_unknown_admin_token_returns_401(client, method, path, body):
    resp = await client.request(method, path, json=body, headers=_admin_headers("nonexistent-token"))
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", _ADMIN_ROUTES)
async def test_revoked_admin_token_returns_401(client, method, path, body, revoked_admin_token):
    resp = await client.request(
        method, path, json=body, headers=_admin_headers(revoked_admin_token["token"])
    )
    assert resp.status_code == 401


# --- POST /admin/teams: creates team + working API key ------------------------


@pytest.mark.asyncio
async def test_create_team_returns_working_api_key(client, db_pool, admin_token):
    resp = await client.post(
        "/admin/teams",
        json={
            "name": "New Team",
            "allowed_models": ["gpt-4o-mini"],
            "rpm_limit": 60,
            "tpm_limit": 10000,
            "daily_budget_usd": "5.00",
            "monthly_budget_usd": "100.00",
        },
        headers=_admin_headers(admin_token["token"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    team_id = body["team_id"]
    api_key = body["api_key"]
    assert team_id
    assert api_key

    try:
        # This step's AC only brings up postgres/redis (no mock-openai), so
        # round-tripping through team_auth.py's lookup is verified directly
        # rather than by expecting a full 200 from a live provider call.
        team = await get_current_team(authorization=f"Bearer {api_key}")
        assert team.id == team_id
        assert team.rpm_limit == 60

        audit_row = await db_pool.fetchrow(
            "SELECT * FROM audit_log WHERE team_id = $1 AND action = 'create_team'", team_id
        )
        assert audit_row is not None
        assert audit_row["admin_name"] == admin_token["admin_name"]
    finally:
        await db_pool.execute("DELETE FROM audit_log WHERE team_id = $1", team_id)
        await db_pool.execute("DELETE FROM spend_ledger WHERE team_id = $1", team_id)
        await db_pool.execute("DELETE FROM team_api_keys WHERE team_id = $1", team_id)
        await db_pool.execute("DELETE FROM teams WHERE id = $1", team_id)


# --- PATCH /admin/teams/{id}/limits: partial update + audit log ---------------


@pytest.mark.asyncio
async def test_update_team_limits_partial_update_and_audit_log(client, db_pool, admin_token, seeded_team):
    team_id = seeded_team["team_id"]
    try:
        resp = await client.patch(
            f"/admin/teams/{team_id}/limits",
            json={"rpm_limit": 999},
            headers=_admin_headers(admin_token["token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["rpm_limit"] == 999

        row = await db_pool.fetchrow(
            "SELECT rpm_limit, tpm_limit FROM teams WHERE id = $1", team_id
        )
        assert row["rpm_limit"] == 999
        assert row["tpm_limit"] == 10000  # untouched

        audit_row = await db_pool.fetchrow(
            "SELECT before, after, admin_name FROM audit_log "
            "WHERE team_id = $1 AND action = 'update_team_limits' ORDER BY id DESC LIMIT 1",
            team_id,
        )
        assert audit_row is not None
        assert audit_row["admin_name"] == admin_token["admin_name"]
        before = json.loads(audit_row["before"])
        after = json.loads(audit_row["after"])
        assert before == {"rpm_limit": 60}
        assert after == {"rpm_limit": 999}
        assert "tpm_limit" not in before
    finally:
        await db_pool.execute("DELETE FROM audit_log WHERE team_id = $1", team_id)


@pytest.mark.asyncio
async def test_update_team_limits_unknown_team_returns_404(client, admin_token):
    resp = await client.patch(
        "/admin/teams/team-does-not-exist/limits",
        json={"rpm_limit": 10},
        headers=_admin_headers(admin_token["token"]),
    )
    assert resp.status_code == 404


# --- GET /admin/teams/{id}/status: rate-limit + budget state ------------------


@pytest.mark.asyncio
async def test_get_team_status_reflects_consumed_capacity(
    client, redis_client, admin_token, seeded_team
):
    team_id = seeded_team["team_id"]

    # rpm_limit=60, realtime ceiling 100% -> capacity 60; consume 3 directly.
    await check_and_consume(
        redis_client,
        f"ratelimit:{team_id}:realtime:rpm",
        capacity=60,
        refill_per_second=60 / 60,
        cost=3,
    )
    # daily_budget_usd=10.00 -> 5.00 spent is 50% utilization.
    await redis_client.set(_daily_key(team_id), "5.00")

    resp = await client.get(
        f"/admin/teams/{team_id}/status", headers=_admin_headers(admin_token["token"])
    )
    assert resp.status_code == 200
    body = resp.json()

    realtime = body["rate_limits"]["realtime"]
    assert realtime["rpm_capacity"] == 60
    assert realtime["rpm_remaining"] == pytest.approx(57, abs=0.1)

    assert body["budget"]["daily_utilization"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_get_team_status_unknown_team_returns_404(client, admin_token):
    resp = await client.get(
        "/admin/teams/team-does-not-exist/status", headers=_admin_headers(admin_token["token"])
    )
    assert resp.status_code == 404


# --- GET /admin/audit-log: ordering + team_id filter ---------------------------


@pytest.mark.asyncio
async def test_audit_log_ordering_and_team_filter(client, db_pool, admin_token, seeded_team):
    team_id = seeded_team["team_id"]
    try:
        await db_pool.execute(
            "INSERT INTO audit_log (admin_name, action, team_id, before, after) VALUES ($1, $2, $3, $4, $5)",
            "someone",
            "manual_seed_1",
            team_id,
            None,
            json.dumps({"x": 1}),
        )
        await db_pool.execute(
            "INSERT INTO audit_log (admin_name, action, team_id, before, after) VALUES ($1, $2, $3, $4, $5)",
            "someone",
            "manual_seed_2",
            team_id,
            None,
            json.dumps({"x": 2}),
        )

        resp = await client.get(
            "/admin/audit-log",
            params={"team_id": team_id},
            headers=_admin_headers(admin_token["token"]),
        )
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        assert len(entries) == 2
        assert entries[0]["action"] == "manual_seed_2"
        assert entries[1]["action"] == "manual_seed_1"
        assert all(e["team_id"] == team_id for e in entries)
        assert entries[0]["after"] == {"x": 2}
    finally:
        await db_pool.execute("DELETE FROM audit_log WHERE team_id = $1", team_id)


# --- POST /admin/config/reload: picks up change, 400s on invalid --------------


@pytest.mark.asyncio
async def test_manual_reload_picks_up_change_and_400s_on_invalid(
    client, admin_token, tmp_path, monkeypatch
):
    original_path = os.environ["PLEXON_CONFIG_PATH"]
    original_content = pathlib.Path(original_path).read_text()

    scratch_path = tmp_path / "reload_test_config.yaml"
    scratch_path.write_text(original_content.replace("failure_threshold: 5", "failure_threshold: 42"))
    monkeypatch.setenv("PLEXON_CONFIG_PATH", str(scratch_path))

    try:
        resp = await client.post(
            "/admin/config/reload", headers=_admin_headers(admin_token["token"])
        )
        assert resp.status_code == 200
        assert get_config().circuit_breaker.failure_threshold == 42

        scratch_path.write_text("not_valid_yaml: [1, 2")
        resp = await client.post(
            "/admin/config/reload", headers=_admin_headers(admin_token["token"])
        )
        assert resp.status_code == 400
        # Failed reload must not have silently kept a half-applied config.
        assert get_config().circuit_breaker.failure_threshold == 42
    finally:
        monkeypatch.setenv("PLEXON_CONFIG_PATH", original_path)
        reload_config()
