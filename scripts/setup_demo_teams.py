"""One-shot script that seeds demo teams directly into Postgres (there's no
admin API bootstrap token to seed through yet -- same constraint
tests/conftest.py's fixtures already work around). Safe to re-run: each team
has a fixed team_id, so re-running updates the same four rows instead of
accumulating duplicates (TRD §9, PRD Core Feature 5).

Run from the host: `python3 scripts/setup_demo_teams.py` (per CLAUDE.md), or
as the one-shot `setup` Docker Compose service (TRD §9).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path

# Runnable as `python3 scripts/setup_demo_teams.py` from any cwd (host or the
# Docker `setup` service) -- that invocation puts this file's own directory
# on sys.path, not the project root, so `import gateway` needs a hand.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.db import close_pool, get_pool, init_pool

# ADR-008: Anthropic/Claude is the preferred provider in practice -- list
# claude-sonnet/claude-opus first in allowed_models wherever present. Purely
# cosmetic for anything demo-facing; resolve_provider_for_model doesn't care
# about list order.
DEMO_TEAMS: list[dict] = [
    {
        "team_id": "demo-realtime-highvolume",
        "name": "Demo: Realtime High Volume",
        "rpm_limit": 600,
        "tpm_limit": 200_000,
        "daily_budget_usd": "50.00",
        "monthly_budget_usd": "1000.00",
        "allowed_models": [
            "claude-sonnet",
            "claude-opus",
            "gpt-4o",
            "gpt-4o-mini",
            "llama3",
            "claude-sonnet--fault-error",
        ],
    },
    {
        "team_id": "demo-batch-lowpriority",
        "name": "Demo: Batch Low Priority",
        "rpm_limit": 60,
        "tpm_limit": 20_000,
        "daily_budget_usd": "5.00",
        "monthly_budget_usd": "100.00",
        "allowed_models": ["claude-sonnet", "gpt-4o-mini", "llama3"],
    },
    {
        "team_id": "demo-tight-budget",
        "name": "Demo: Tight Budget",
        "rpm_limit": 120,
        "tpm_limit": 40_000,
        "daily_budget_usd": "1.00",
        "monthly_budget_usd": "5.00",
        "allowed_models": ["claude-sonnet", "gpt-4o-mini", "llama3"],
    },
    {
        "team_id": "demo-frontier",
        "name": "Demo: Frontier",
        "rpm_limit": 30,
        "tpm_limit": 10_000,
        "daily_budget_usd": "20.00",
        "monthly_budget_usd": "400.00",
        "allowed_models": ["claude-opus", "gpt-4o"],
    },
]

_OUTPUT_PATH = Path(__file__).parent / "demo_teams.json"

# FK-safe delete order: dependents of `teams` before `teams` itself.
_DEPENDENT_TABLES = ("team_api_keys", "spend_ledger", "audit_log", "alert_history")


async def seed_demo_teams() -> list[dict]:
    await init_pool()
    try:
        pool = get_pool()
        seeded = []

        for entry in DEMO_TEAMS:
            team_id = entry["team_id"]

            for table in _DEPENDENT_TABLES:
                await pool.execute(f"DELETE FROM {table} WHERE team_id = $1", team_id)
            await pool.execute("DELETE FROM teams WHERE id = $1", team_id)

            await pool.execute(
                """
                INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit,
                                    daily_budget_usd, monthly_budget_usd)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                team_id,
                entry["name"],
                entry["allowed_models"],
                entry["rpm_limit"],
                entry["tpm_limit"],
                entry["daily_budget_usd"],
                entry["monthly_budget_usd"],
            )

            api_key = secrets.token_urlsafe(32)
            await pool.execute(
                "INSERT INTO team_api_keys (token, team_id) VALUES ($1, $2)", api_key, team_id
            )

            seeded.append(
                {
                    "team_id": team_id,
                    "name": entry["name"],
                    "api_key": api_key,
                    "rpm_limit": entry["rpm_limit"],
                    "tpm_limit": entry["tpm_limit"],
                    "daily_budget_usd": entry["daily_budget_usd"],
                    "monthly_budget_usd": entry["monthly_budget_usd"],
                    "allowed_models": entry["allowed_models"],
                }
            )

        return seeded
    finally:
        await close_pool()


def main() -> None:
    seeded = asyncio.run(seed_demo_teams())

    print(f"{'name':<28} {'team_id':<28} {'api_key':<46} {'rpm/tpm':<14} budgets (daily/monthly)")
    for team in seeded:
        rpm_tpm = f"{team['rpm_limit']}/{team['tpm_limit']}"
        budgets = f"{team['daily_budget_usd']}/{team['monthly_budget_usd']}"
        print(
            f"{team['name']:<28} {team['team_id']:<28} {team['api_key']:<46} {rpm_tpm:<14} {budgets}"
        )

    _OUTPUT_PATH.write_text(json.dumps(seeded, indent=2) + "\n")
    print(f"\nWrote {len(seeded)} teams to {_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
