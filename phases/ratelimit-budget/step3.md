# Step 3: admin-api

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ADR.md` — ADR-012 (team keys and admin tokens are two fully independent opaque-token systems — never a single token encoding both; tokens are random opaque strings looked up in a table, not self-encoding like JWTs; revocation is just deleting/marking a row)
- `/docs/TRD.md` — §6.2 (Admin API endpoint table — the exact six endpoints this step implements), §4.1 (`admin_tokens` and `audit_log` table schemas)
- `/docs/ARCHITECTURE.md` — directory layout (`gateway/admin/` — already exists as an empty package from `proxy-layer` step 0)
- `gateway/auth/team_auth.py` — the `get_current_team` pattern (`Header`-based lookup, generic 401 on any failure, no in-process cache per ADR-007) — mirror this shape exactly for admin auth, don't invent a different pattern
- `phases/ratelimit-budget/step1.md`'s actual output: `gateway/ratelimit/limiter.py` (for reading current bucket state in the status endpoint — you'll need read-only visibility into a team's current rpm/tpm bucket remaining-capacity, per tier)
- `phases/ratelimit-budget/step2.md`'s actual output: `gateway/ratelimit/budget.py` (`check_budget`, Redis spend key scheme — reuse for the spend endpoint)
- `gateway/config/loader.py` — `get_config()` and `start_config_watcher()` (you'll add a manual reload path alongside the existing file-watch one)
- `gateway/db.py` — `get_pool()`
- `deploy/schema.sql` — `admin_tokens`, `audit_log`, `teams`, `team_api_keys` table schemas exactly

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

### 1. Admin authentication — `gateway/auth/admin_auth.py`

Mirror `gateway/auth/team_auth.py`'s exact shape:

```python
class Admin(BaseModel):
    name: str

async def get_current_admin(authorization: str | None = Header(default=None)) -> Admin: ...
```

Same rules as team auth: `Authorization: Bearer <token>` or bare token, direct Postgres lookup against `admin_tokens` on every call (no cache), `revoked_at IS NULL` required, unknown/missing/revoked all collapse to the same generic `401` (don't leak which case it was).

### 2. Admin routes — `gateway/admin/routes.py`

Implement TRD §6.2's six endpoints, each behind `Depends(get_current_admin)`:

```python
router = APIRouter(prefix="/admin")

@router.get("/teams/{team_id}/status")
async def get_team_status(team_id: str, admin: Admin = Depends(get_current_admin)) -> dict: ...
# Current rate-limit/budget status: remaining rpm/tpm bucket capacity per tier
# (read-only -- call step 1's bucket-reading path, don't consume from it),
# plus current daily/monthly spend utilization via step 2's check_budget.

@router.patch("/teams/{team_id}/limits")
async def update_team_limits(team_id: str, body: ..., admin: Admin = Depends(get_current_admin)) -> dict: ...
# Live-updates rpm_limit/tpm_limit/daily_budget_usd/monthly_budget_usd on the
# teams row (partial update -- only fields present in the request body change).
# Writes an audit_log row: admin_name, action="update_team_limits", team_id,
# before (prior values as jsonb), after (new values as jsonb).

@router.get("/teams/{team_id}/spend")
async def get_team_spend(team_id: str, admin: Admin = Depends(get_current_admin)) -> dict: ...
# Spend dashboard data: aggregate spend_ledger rows for the team (e.g. total
# cost/tokens by day over some reasonable recent window, and by provider/model).

@router.post("/teams")
async def create_team(body: ..., admin: Admin = Depends(get_current_admin)) -> dict: ...
# Creates a teams row from the request body (name, allowed_models, rpm_limit,
# tpm_limit, daily_budget_usd, monthly_budget_usd). Writes an audit_log row
# (action="create_team"). See the API-key generation rule below.

@router.get("/audit-log")
async def list_audit_log(team_id: str | None = None, admin: Admin = Depends(get_current_admin)) -> dict: ...
# Query audit_log, optionally filtered by team_id, most recent first,
# reasonably paginated/limited (e.g. most recent 100 -- no need for full
# cursor-based pagination for this project's scope).

@router.post("/config/reload")
async def reload_config(admin: Admin = Depends(get_current_admin)) -> dict: ...
# Manually triggers the same YAML reload the file-watcher already does.
```

Wire `router` into `gateway/main.py` alongside the existing gateway `router`.

**Design decision already made (don't re-litigate): `POST /admin/teams` also generates an API key.** TRD §6.2 defines no separate endpoint for issuing a team's first `team_api_keys` row, and a team with zero keys is unusable. Generate one opaque token (`secrets.token_urlsafe(32)` or similar — random, not self-encoding, per ADR-012), insert it into `team_api_keys` for the new team, and return it in the response body. This is the only place a key gets minted in this step; don't build a general key-rotation/reissuance endpoint beyond it — that's out of scope.

**`config/reload`'s core rule.** `gateway/config/loader.py` currently only reloads via its internal file-watch loop, which mutates the module-level `_config` global directly. Add a `reload_config() -> None` function to `loader.py` that does the same `_load_from_disk()` + assignment the watch loop does (refactor the watch loop to call this shared function rather than duplicating the reload logic), so both the file-watcher and this new manual endpoint go through one code path. On a YAML parse/validation failure, the manual reload should raise a clear error back to the admin caller (e.g. `400` with the validation message) rather than silently keeping the old config the way the background watch loop does — an admin who explicitly asked for a reload should know it failed, unlike the passive file-watcher which just logs and moves on.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build postgres redis
uv run pytest tests/test_admin.py -v
docker compose -f deploy/docker-compose.yml down
```

`tests/test_admin.py` must cover, against real Postgres (add an `admin_token` fixture to `tests/conftest.py` mirroring `seeded_team`'s pattern — insert directly into `admin_tokens`, no admin endpoint exists yet to create one through):
- Missing/unknown/revoked admin token all return `401` on every admin route (parametrize across all six endpoints, or at least a representative sample).
- `POST /admin/teams` creates a team, returns a usable API key, and that key immediately works against `POST /v1/chat/completions` (an end-to-end check that the generated key round-trips through `team_auth.py`'s lookup).
- `PATCH /admin/teams/{id}/limits` updates only the fields provided, leaves others untouched, and writes an `audit_log` row with correct `before`/`after` values.
- `GET /admin/teams/{id}/status` reflects rate-limit/budget state correctly for a team with known consumed capacity (seed some usage first, then check the reported numbers match).
- `GET /admin/audit-log` returns entries in most-recent-first order and correctly filters by `team_id` when provided.
- `POST /admin/config/reload` picks up an on-disk change to the test config file and returns `400` (not a silent no-op) if the file is made invalid before reloading.

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Is admin auth a fully independent lookup from team auth — no shared token table, no single token valid for both (ADR-012)?
   - Does every mutating admin action (limits update, team creation) write a correctly-populated `audit_log` row?
   - Is the API-key-generation addition to `POST /admin/teams` the *only* place this step mints a team key (no separate rotation endpoint invented beyond what was asked)?
3. Based on the result, update `phases/ratelimit-budget/index.json` step 3:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files/routes added, the API-key-generation decision, the config/reload refactor -- marking ratelimit-budget phase complete"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't let admin and team auth share any table, token format, or lookup code path. Reason: ADR-012 requires them to be fully independent systems.
- Don't build a general API-key rotation/reissuance/revocation endpoint. Reason: not asked for by TRD §6.2 or the PRD; `POST /admin/teams` minting one initial key is the one documented gap being filled here, nothing more.
- Don't let the manual `/admin/config/reload` endpoint silently keep the old config on a validation failure the way the background file-watcher does. Reason: an admin explicitly requesting a reload needs to know it failed; a passive background watcher failing silently and an active API call failing silently are different situations.
- Don't duplicate the YAML reload logic between the file-watcher and the manual endpoint. Reason: single source of truth for what "reload" means.
- Do not break existing tests.
