# Step 0: project-setup

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — full product scope, especially "Core Features 1: Unified Proxy Layer" and "Architecture Overview"
- `/docs/ARCHITECTURE.md` — directory structure, state management (Redis vs Postgres vs YAML)
- `/docs/ADR.md` — read ADR-001 (Python/FastAPI), ADR-004 (Redis vs Postgres split), ADR-005 (YAML vs Postgres config split), ADR-006 (mocked OpenAI/Anthropic, real Ollama), ADR-007 (stateless gateway), ADR-023 (uv packaging)
- `/docs/TRD.md` — §1 (tech stack), §4 (data model — both the Postgres schema in §4.1 and the Redis key schema in §4.2), §5 (YAML config shape), §9 (Docker Compose services), §11 (proposed project structure)
- `/CLAUDE.md` — project conventions and commands

There is no existing gateway code yet — this is the first step of the first phase. `scripts/execute.py` (the Harness runner) and `scripts/setup_demo_teams.py` placeholder already exist; don't modify `scripts/execute.py`.

## Task

Set up the project skeleton, local dev environment, and Postgres schema. Nothing in this step implements gateway logic — later steps fill in the modules created here.

### 1. Python packaging (`uv`, ADR-023)

Create `pyproject.toml` at the repo root:
- Project name `plexon`, Python `>=3.11`.
- Runtime dependencies: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pyyaml`, `watchfiles` (YAML hot-reload), `asyncpg`, `redis` (the `redis.asyncio` client), `httpx` (provider HTTP calls).
- Dev dependencies (a `[dependency-groups]` or `[tool.uv]` dev group): `pytest`, `pytest-asyncio`, `ruff`.
- Configure `ruff` with a `[tool.ruff]` section (line length 100 is fine — use your judgement, this isn't specified anywhere).

Run `uv sync` to generate `uv.lock` and a `.venv`. Do not commit `.venv/` — check `.gitignore` already excludes it (it should; if not, add it).

### 2. Directory skeleton

Create the structure from `docs/ARCHITECTURE.md`'s "Directory Structure" section:

```
gateway/
├── __init__.py
├── main.py
├── auth/__init__.py
├── ratelimit/__init__.py
├── providers/__init__.py
├── resilience/__init__.py
├── enrichment/__init__.py
├── admin/__init__.py
├── observability/__init__.py
└── config/__init__.py
mocks/__init__.py
tests/
├── __init__.py
├── integration/__init__.py
└── load/__init__.py
deploy/
├── docker-compose.yml
├── schema.sql
├── grafana/
└── prometheus/
```

Only create `__init__.py` files and empty directories for modules that later steps will fill in (`grafana/`, `prometheus/` can just be empty dirs with a `.gitkeep` — they're populated in the `observability` phase). Do not put any logic in these `__init__.py` files beyond what's needed to make the package importable.

### 3. Postgres schema (`deploy/schema.sql`)

Write out the **full** schema from `docs/TRD.md` §4.1 verbatim (all seven tables: `teams`, `team_api_keys`, `admin_tokens`, `spend_ledger`, `audit_log`, `circuit_breaker_history`, `provider_health_history`), even though this phase (`proxy-layer`) only reads/writes `teams` and `team_api_keys`. Defining the whole schema now means later phases (`ratelimit-budget`, `resilience`) add code against tables that already exist, instead of patching this file repeatedly.

This is a plain `.sql` file (per the project's decision: raw `asyncpg` + hand-written SQL, no ORM/migration tool for now) — it gets applied by Postgres's docker-entrypoint-initdb mechanism (see compose config below), not by application code.

### 4. YAML config (`gateway/config/`)

- `config.yaml` at the repo root (or `deploy/config.yaml` — pick one location and be consistent; `gateway/config/loader.py` needs to know where to find it, e.g. via a `PLEXON_CONFIG_PATH` env var defaulting to `./config.yaml`). Populate it with the **full** shape from `docs/TRD.md` §5 (`providers`, `fallback_chains`, `circuit_breaker`, `health_check`, `priority_tiers`, `enrichment_defaults`, `pricing`) — use the TRD's example values as-is, adjusted so `providers.openai.base_url` / `providers.anthropic.base_url` point at the mock service hostnames used in the compose file (e.g. `http://mock-openai:8080`, `http://mock-anthropic:8080`), and `providers.ollama.base_url` at `http://ollama:11434` (the Ollama service itself isn't part of this phase's compose file — see below).
- `gateway/config/loader.py`: a module that loads and validates this YAML into a Pydantic model (e.g. `GatewayConfig`, mirroring the TRD §5 structure — nested models for `ProviderConfig`, `FallbackChains`, `CircuitBreakerConfig`, `HealthCheckConfig`, `PriorityTierConfig`, `EnrichmentDefaults`, `PricingConfig`) and watches the file for changes using `watchfiles`, swapping in the reloaded config atomically. Expose a single module-level accessor, e.g.:

```python
def get_config() -> GatewayConfig: ...
def start_config_watcher() -> None: ...  # call once at app startup
```

Only `providers` and `pricing` are actually consumed by this phase's code — the rest are loaded and validated now so later phases don't need to touch the loader.

### 5. Postgres pool (`gateway/db.py`)

A thin module wrapping an `asyncpg` connection pool:

```python
async def init_pool() -> None: ...   # called on FastAPI startup, reads DSN from env (PLEXON_DATABASE_URL)
async def close_pool() -> None: ...  # called on FastAPI shutdown
def get_pool() -> asyncpg.Pool: ...  # raises if init_pool() hasn't run
```

Don't add query helper methods yet — the `auth` step (step 4) is the first real consumer and will add what it needs.

### 6. Minimal FastAPI app (`gateway/main.py`)

- Instantiate the `FastAPI` app.
- Wire `init_pool`/`close_pool` and `start_config_watcher` into FastAPI's lifespan.
- `GET /healthz` → `{"status": "ok"}`, no auth (per `docs/TRD.md` §6.1). This is the only route in this step.

### 7. Docker Compose (`deploy/docker-compose.yml`)

Services: `gateway`, `redis`, `postgres`, `mock-openai`, `mock-anthropic`.

- `postgres`: official `postgres:16` image, mounts `./schema.sql` into `/docker-entrypoint-initdb.d/`, exposes standard env vars (`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` — pick fixed demo values, this isn't a security-sensitive deployment per ADR-002).
- `redis`: official `redis:7` image. Nothing reads/writes it yet (that starts in the `ratelimit-budget` phase) — it's included now so the compose topology is complete and stable across phases.
- `mock-openai` / `mock-anthropic`: build from `./mocks/Dockerfile.openai` and `./mocks/Dockerfile.anthropic` (or a shared `mocks/Dockerfile` parameterized by an entrypoint arg — your choice). These Dockerfiles and the apps they run **don't exist yet** — step 2 (`mock-providers`) creates them. It's fine for `docker compose build` to fail on these two services until step 2 lands; don't stub out fake mock apps here just to make compose fully buildable early — that would duplicate step 2's work.
- `gateway`: builds from a root `Dockerfile` (create a simple one: `python:3.11-slim` base, install `uv`, `uv sync --frozen`, `CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]`), depends on `postgres` and `redis`, reads `PLEXON_DATABASE_URL` and `PLEXON_CONFIG_PATH` from environment.

Do not add `prometheus`, `grafana`, `tempo`, `ollama`, or `setup` services yet — those belong to later phases per `docs/TRD.md` §12's phase plan.

## Acceptance Criteria

```bash
uv sync                                    # installs cleanly, produces uv.lock
uv run ruff check .                        # no lint errors
uv run pytest                              # passes (even with zero tests collected)
docker compose -f deploy/docker-compose.yml up -d postgres redis   # both start healthy
docker compose -f deploy/docker-compose.yml exec -T postgres psql -U <user> -d <db> -c '\dt'   # lists all 7 tables from schema.sql
uv run uvicorn gateway.main:app --port 8000 &
sleep 1 && curl -sf http://localhost:8000/healthz   # {"status": "ok"}
kill %1
docker compose -f deploy/docker-compose.yml down -v
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does the directory layout match `docs/ARCHITECTURE.md` exactly?
   - Does `deploy/schema.sql` match `docs/TRD.md` §4.1 table-for-table, column-for-column?
   - Is Postgres used only for durable state and Redis left untouched by application code (ADR-004)?
   - Is all config in this step's `config.yaml` global/static, with nothing per-team (ADR-005)?
3. Based on the result, update `phases/proxy-layer/index.json` step 0:
   - Success → `"status": "completed"`, `"summary": "one-line summary of what was created (files/paths), for the next step to build on"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed (e.g. Docker not available) → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't implement any gateway request-handling logic beyond `/healthz`. Reason: routing/auth/enrichment are later steps — doing them here breaks the one-module-per-step design.
- Don't add `prometheus`/`grafana`/`tempo`/`ollama` services to compose. Reason: out of scope for this phase (TRD §12); adding them now creates services nothing configures until much later.
- Don't stub fake mock-provider apps to make `docker compose build` fully green. Reason: that duplicates step 2's work and risks diverging from ADR-025's fault-injection contract.
- Don't add a migration tool (Alembic, etc.). Reason: explicit decision to use plain SQL for now; revisit only if the project later moves to an ORM.
- Do not break existing tests (`scripts/test_execute.py` — don't touch it, it's the Harness runner's own test suite, unrelated to gateway code).
