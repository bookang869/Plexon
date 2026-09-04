# Step 0: demo-teams-script

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 5 ("Full stack containerized via Docker Compose... Setup script creates demo teams with varied rate limits/priorities.")
- `/docs/TRD.md` — §9 (deployment service list: `setup` — "one-shot script creating demo teams with varied rate limits/priorities"), §11 (project structure: `scripts/setup_demo_teams.py` is the exact intended path)
- `/docs/ADR.md` — ADR-004 (Postgres is durable state for team configs), ADR-008 (Anthropic/Claude is the preferred provider in practice — "default provider in demo team configs"), ADR-023 (`uv run` is how project scripts execute)
- `CLAUDE.md` (project root) — Commands section already documents `python3 scripts/setup_demo_teams.py` as a standalone command
- `deploy/schema.sql` — exact `teams`/`team_api_keys` column shapes you're inserting into
- `gateway/db.py` — `init_pool`/`get_pool`/`close_pool`, the only Postgres access pattern used anywhere in this codebase; reuse it rather than opening `asyncpg` connections ad hoc
- `config.yaml` (project root) — the real provider `models` lists and `pricing` table your demo teams' `allowed_models` should draw from
- `tests/conftest.py` — `seeded_team`/`revoked_team` fixtures: the direct-INSERT-via-pool pattern this script mirrors (there's no admin API bootstrap token yet, so this script inserts directly, the same way tests do)
- `deploy/docker-compose.yml` — current service list; you're adding one more (`setup`)
- `Dockerfile` (project root) — current build only `COPY`s `gateway` and `config.yaml`; you're adding `scripts` too

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

A standalone, idempotent script that seeds demo teams directly into Postgres (there's no admin API bootstrap token to seed through yet — same constraint `conftest.py`'s fixtures already work around), runnable both from the host (per CLAUDE.md's documented `python3 scripts/setup_demo_teams.py`) and as a one-shot Docker Compose service (per TRD §9).

### 1. `scripts/setup_demo_teams.py`

```python
DEMO_TEAMS: list[dict] = [
    # name, rpm_limit, tpm_limit, daily_budget_usd, monthly_budget_usd, allowed_models
    ...
]

async def seed_demo_teams() -> list[dict]:
    """Connects via gateway.db.init_pool()/get_pool() (PLEXON_DATABASE_URL from
    env, same convention as every other entrypoint in this codebase). For each
    entry in DEMO_TEAMS: deletes any existing team with that fixed team_id (and
    its dependent rows -- team_api_keys, spend_ledger, audit_log,
    alert_history, in FK-safe order, same tables tests/conftest.py's fixtures
    tear down) so re-running is safe, then inserts a fresh team + a freshly
    generated API key. Returns the list of {team_id, name, api_key, rpm_limit,
    tpm_limit, daily_budget_usd, monthly_budget_usd, allowed_models} dicts."""

def main() -> None:
    """Runs seed_demo_teams() via asyncio.run, prints a human-readable table
    to stdout (team name, team_id, api_key, rpm/tpm, budgets), and writes the
    same data as JSON to scripts/demo_teams.json (step 4's Locust scenario
    reads this file for its pool of team credentials)."""
```

**Team roster — four teams with genuinely different rate-limit/budget profiles** (PRD: "varied rate limits/priorities"), fixed `team_id`s (not random UUIDs, so re-running the script updates the same rows instead of accumulating duplicates) prefixed `demo-`:

1. `demo-realtime-highvolume` — generous rpm/tpm (e.g. 600/200000), generous budget (e.g. daily 50.00 / monthly 1000.00), `allowed_models` spanning all five configured models.
2. `demo-batch-lowpriority` — modest rpm/tpm (e.g. 60/20000), modest budget, `allowed_models` a smaller subset (e.g. `claude-sonnet`, `gpt-4o-mini`, `llama3`) — meant to be driven with the `X-Priority: batch` header in the demo/load test.
3. `demo-tight-budget` — mid rpm/tpm but a deliberately small budget (e.g. daily 1.00 / monthly 5.00) so budget-cap enforcement (402) is easy to trigger quickly in a live demo.
4. `demo-frontier` — small rpm/tpm (e.g. 30/10000), `allowed_models` restricted to the frontier tier (`claude-opus`, `gpt-4o`).

Per ADR-008, list `claude-sonnet`/`claude-opus` first within each team's `allowed_models` (Anthropic is the preferred provider in practice) — this doesn't change any routing behavior (`resolve_provider_for_model` doesn't care about list order), it's just consistent with the ADR's framing for anything demo-facing.

Read `config.yaml`'s `providers.*.models` lists yourself rather than hardcoding model names independent of it — if the real list is `["gpt-4o", "gpt-4o-mini"]` / `["claude-opus", "claude-sonnet"]` / `["llama3"]`, that's what `allowed_models` should draw from.

### 2. `.gitignore`

Add `scripts/demo_teams.json` — it contains freshly generated API keys (mock/demo credentials, but still generated secrets that shouldn't accumulate as noisy diffs in version control).

### 3. `Dockerfile` (project root)

Add `COPY scripts ./scripts` alongside the existing `COPY gateway ./gateway` / `COPY config.yaml ./config.yaml` lines — the current image builds `--no-dev` and never copies `scripts/`, so the one-shot `setup` service below has nothing to run without this.

### 4. `deploy/docker-compose.yml`

Add a `setup` service per TRD §9: same `build: {context: .., dockerfile: Dockerfile}` as `gateway`, `depends_on: postgres: {condition: service_healthy}`, `environment: PLEXON_DATABASE_URL` pointed at the in-network `postgres:5432` host (same value the `gateway` service already uses), `command: ["python3", "scripts/setup_demo_teams.py"]`, and `restart: "no"` (it's a one-shot job, not a long-running service — don't let Compose treat a clean exit-0 as a crash to restart).

## Acceptance Criteria

```bash
docker compose -f deploy/docker-compose.yml up -d postgres
uv run python3 scripts/setup_demo_teams.py
uv run python3 scripts/setup_demo_teams.py   # re-run: must not error (idempotent)
python3 -c "import json; d = json.load(open('scripts/demo_teams.json')); assert len(d) == 4; assert all('api_key' in t and 'team_id' in t for t in d)"
docker compose -f deploy/docker-compose.yml config -q   # validates the new `setup` service parses
docker compose -f deploy/docker-compose.yml down
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does the script use `gateway.db.init_pool`/`get_pool`/`close_pool` rather than opening its own ad hoc `asyncpg` connection? (Reuse existing mechanisms, ADR-011's spirit.)
   - Is every team's `team_id` fixed/deterministic (not `uuid.uuid4()`) so re-running the script updates rather than duplicates?
   - Does the delete-before-insert path clean up every FK-dependent table (`team_api_keys`, `spend_ledger`, `audit_log`, `alert_history`) before deleting the `teams` row, in that order?
3. Based on the result, update `phases/test-load/index.json` step 0:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created, the four demo teams' names/profiles, scripts/demo_teams.json's shape (so step 4's Locust file knows what to read), the setup Compose service"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't generate `team_id`s randomly (e.g. `uuid.uuid4()`). Reason: this script must be safe to re-run (Docker Compose runs it on every `up`, and a human might re-run it manually) — a random ID would leave orphaned rows behind on every re-run instead of refreshing the same four demo teams.
- Don't create an admin token or route team creation through the admin API. Reason: there's no admin-token bootstrap mechanism yet, and building one is out of scope for this step — direct-pool insertion (the same technique every test fixture already uses) is the established pattern here.
- Don't hardcode model names independent of `config.yaml`. Reason: if a future step changes the provider model lists, a hardcoded roster silently drifts out of sync and demo teams end up "allowed" to request models no provider actually serves.
- Do not break existing tests.
