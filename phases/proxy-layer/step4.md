# Step 4: auth

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ADR.md` — read ADR-012 (separate team-key and admin-token systems) closely
- `/docs/TRD.md` — §4.1 (the `teams`/`team_api_keys`/`admin_tokens` tables — already created by step 0's `deploy/schema.sql`), §6.1/§6.2 (which routes use which auth — this phase only needs team-key auth; admin-token auth is built in the `ratelimit-budget` phase when admin routes are added)
- `phases/proxy-layer/step0.md`'s actual output: `deploy/schema.sql` (exact column names/types for `teams`/`team_api_keys`), `gateway/db.py` (the asyncpg pool accessor this step uses)

## Task

Implement team API key authentication as a FastAPI dependency, plus the minimal seed data needed to test it.

### `gateway/auth/team_auth.py`

```python
class Team(BaseModel):
    id: str
    name: str
    allowed_models: list[str]
    rpm_limit: int
    tpm_limit: int
    daily_budget_usd: Decimal | None
    monthly_budget_usd: Decimal | None
    config: dict  # the teams.config jsonb column, raw — enrichment step (step 5) interprets its contents

async def get_current_team(authorization: str = Header(...)) -> Team: ...  # FastAPI dependency
```

Rules that must not be violated:
- The `Authorization` header carries the raw team API key (e.g. `Authorization: Bearer <key>` — strip the `Bearer ` prefix if present, but also accept the bare key for simplicity, your call). Look it up against `team_api_keys.token` (only rows where `revoked_at IS NULL`), join to `teams` for the full config.
- An unknown, missing, or revoked key → `401 Unauthorized` with a generic error body (don't leak whether the key exists but is revoked vs. never existed — that's a minor but real distinction worth getting right).
- This dependency does **not** implement admin-token auth. Per ADR-012, team keys and admin tokens are two independent systems with separate lookup tables — don't build a combined "try team key, then try admin token" fallback. Admin auth is a separate dependency added in a later phase when `/admin/*` routes exist.
- Don't cache team lookups in-process (e.g. a module-level dict). Reason: this violates ADR-007's stateless-gateway rule in spirit (a stale in-memory cache after an admin revokes a key would let it keep working) — every request does a real Postgres lookup. If lookup latency ever becomes a demonstrated problem, that's a Redis-cache decision for later, not an assumption to bake in now.

### Seed data for testing (`tests/conftest.py` or a dedicated fixture module)

Add a pytest fixture that inserts a test team + team API key directly via `gateway/db.py`'s pool (not through an admin API — that doesn't exist yet) into the Postgres instance from `docker-compose.yml`, and tears it down after the test. This is the first step whose tests need real Postgres running — its AC reflects that.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d postgres
uv run pytest tests/test_auth.py -v
# tests/test_auth.py must cover:
#   - valid key -> Team returned with correct fields
#   - missing Authorization header -> 401
#   - unknown key -> 401
#   - revoked key (revoked_at set) -> 401
docker compose -f deploy/docker-compose.yml down
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Is team-key lookup a genuine per-request Postgres query, with no in-process caching?
   - Is this step free of any admin-token logic (ADR-012's separation)?
   - Does the `Team` model's `config` field stay untyped/raw here, leaving interpretation to the `enrichment` step?
3. Based on the result, update `phases/proxy-layer/index.json` step 4:
   - Success → `"status": "completed"`, `"summary": "one-line summary — dependency name/import path, Team model shape, for routing/enrichment steps"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't build any admin-token auth or `/admin/*` routes in this step. Reason: out of scope for `proxy-layer` (TRD §12) — that's the `ratelimit-budget` phase.
- Don't add an in-process cache for team lookups. Reason: explained above — correctness (immediate key revocation) over micro-optimizing a lookup that isn't yet a demonstrated bottleneck.
- Don't implement rate-limit or budget checks here even though the `Team` model carries `rpm_limit`/`tpm_limit`/budget fields. Reason: those fields are read from Postgres now because they're part of the `teams` row, but enforcing them is the `ratelimit-budget` phase's job — this step only authenticates and returns the team's config.
- Do not break existing tests.
