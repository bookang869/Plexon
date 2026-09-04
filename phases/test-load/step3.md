# Step 3: ci-workflow

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 5 ("CI (GitHub Actions) runs the integration suite against real Redis/Postgres service containers, not in-memory substitutes")
- `/docs/TRD.md` — §10 ("CI: test suite runs with no external secrets (mocked providers); Redis/Postgres run as real GitHub Actions service containers (ADR-022)")
- `/docs/ADR.md` — ADR-006 (providers mocked with fault injection — the whole suite runs with zero real API keys/network calls), ADR-022 (real Redis/Postgres as GitHub Actions service containers, not `fakeredis`/SQLite — "Tests exercise the actual Lua/SQL the gateway runs in production"), ADR-023 (`uv` for dependency management/lockfile)
- `CLAUDE.md` (project root) — Commands section (`pytest`, `ruff check .`) and "CRITICAL: OpenAI and Anthropic providers are mocked with fault injection in tests — the test suite must never require real API keys or network calls"
- `tests/conftest.py` — the exact env-var defaults every test file relies on: `PLEXON_DATABASE_URL` defaults to `postgresql://plexon:plexon@localhost:5433/plexon`, `PLEXON_REDIS_URL` defaults to `redis://localhost:6379/0`. **Match these exact host ports in the CI service containers below** rather than overriding the env vars — it's one less thing to keep in sync with `conftest.py` if it ever changes.
- `deploy/schema.sql` — the schema CI's Postgres service container needs applied before tests run (unlike `docker-compose.yml`, a bare GitHub Actions service container has no equivalent of the `docker-entrypoint-initdb.d` auto-init mount, so this has to be an explicit step)
- `deploy/docker-compose.yml` — the `mock-openai`/`mock-anthropic` services and their Dockerfiles (`mocks/Dockerfile.openai`, `mocks/Dockerfile.anthropic`) — CI needs these running too, since several existing route-level tests (`test_routing.py`, `test_streaming.py`, `test_budget.py`, this phase's `tests/integration/`) hit them over real HTTP on `localhost:8081`/`localhost:8082`.
- `pyproject.toml` — current `dependencies`/`[dependency-groups].dev` (`pytest`, `pytest-asyncio`, `ruff`) — this is what `uv sync` installs in CI.
- `phases/test-load/step0.md`'s actual output — confirm whether `scripts/setup_demo_teams.py` exists yet (it's step 0 of this same phase, executed before this step); CI does not need to run it, but shouldn't fail if it's present.

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

Create `.github/workflows/ci.yml`. Triggers: `push` and `pull_request` on any branch (this repo's convention so far is one `feat-{phase}` branch per Harness phase — the workflow should validate all of them, not just `main`).

### Job shape

One job, `test`, running on `ubuntu-latest`:

1. **Service containers** (GitHub Actions native `services:` block, per ADR-022 — not started manually with `docker run`):
   - `postgres`: image `postgres:16`, env `POSTGRES_DB: plexon`, `POSTGRES_USER: plexon`, `POSTGRES_PASSWORD: plexon` (matching `deploy/docker-compose.yml`'s postgres service exactly), `ports: ["5433:5432"]` (host port `5433` to match `conftest.py`'s default, container port `5432`), with a `pg_isready` health check (`options: --health-cmd pg_isready --health-interval 5s --health-timeout 5s --health-retries 10`).
   - `redis`: image `redis:7`, `ports: ["6379:6379"]`, health check via `redis-cli ping`.

2. **Checkout** (`actions/checkout@v4`).

3. **Install `uv`** (`astral-sh/setup-uv@v3` or equivalent) and run `uv sync` (installs the dev group too — don't use `--no-dev` here, `pytest`/`ruff` are dev dependencies).

4. **Apply the schema** to the Postgres service container: `PGPASSWORD=plexon psql -h localhost -p 5433 -U plexon -d plexon -f deploy/schema.sql` (GitHub's `ubuntu-latest` runner image ships a `psql` client already; don't add an `apt-get install` step for it unless this actually fails).

5. **Build and start the mock providers** so route-level tests have real HTTP endpoints to hit: `docker compose -f deploy/docker-compose.yml up -d --build mock-openai mock-anthropic`, then a short wait/health-check loop before proceeding (these containers have no explicit healthcheck defined in `docker-compose.yml` today — poll their `/v1/chat/completions` or root path with `curl --retry` until they respond, or just `sleep` a few seconds; don't add a Compose-level healthcheck to `mock-openai`/`mock-anthropic` as part of this step, that's out of scope — a workflow-level retry loop is enough).

6. **Lint**: `uv run ruff check .`

7. **Test**: `uv run pytest -v` (this single invocation covers unit tests in `tests/*.py` and the integration tests in `tests/integration/*.py` this phase's earlier steps added — there's no separate marker/split needed, `pytest`'s default discovery already finds both).

8. **Teardown**: `docker compose -f deploy/docker-compose.yml down` (`if: always()`, so it runs even if the test step failed).

Set `env: PLEXON_CONFIG_PATH` at the job or step level to `tests/fixtures/test_config.yaml`'s path if `conftest.py`'s own `os.environ.setdefault` doesn't already cover it reliably in a fresh CI shell — check `conftest.py` first; if its `setdefault` already resolves correctly relative to the repo root when `pytest` runs from the repo root (it does — `os.path.dirname(__file__)`), no extra env var is needed in the workflow.

## Acceptance Criteria

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"   # valid YAML
uv run ruff check .
```

There's no local tool to fully validate GitHub Actions workflow syntax/semantics (no `act`/`actionlint` available in this environment — confirmed absent) beyond YAML well-formedness — the authoritative check happens when this branch's commits are actually pushed and the workflow runs on GitHub. Read the file back after writing it and sanity-check: every `uses:` action reference has a real, current major-version tag (e.g. `actions/checkout@v4`, not a made-up version), the `postgres`/`redis` service blocks' `ports` mappings match `conftest.py`'s expected `5433`/`6379`, and step ordering makes sense (schema applied before `pytest` runs, mocks up before `pytest` runs, teardown has `if: always()`).

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Are Postgres/Redis genuine GitHub Actions `services:` containers (per ADR-022), not started via a manual `docker run` step?
   - Does the workflow apply `deploy/schema.sql` before running tests? (A GitHub Actions service container has no init-script mount — this must be an explicit step.)
   - Does the workflow require zero secrets/API keys? (Per ADR-006 — nothing in this file should reference `secrets.*` for a provider API key.)
3. Based on the result, update `phases/test-load/index.json` step 3:
   - Success → `"status": "completed"`, `"summary": "one-line summary — file created, service container port mapping, schema-apply approach, confirmation no secrets are required"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't reference any `secrets.*` for a provider API key. Reason: ADR-006/CLAUDE.md's CRITICAL rule — the whole suite runs against mocked providers; a workflow that expects a real `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` secret would break for anyone forking the repo without those secrets configured, defeating the point.
- Don't use `fakeredis` or an in-memory Postgres substitute to sidestep starting real service containers. Reason: ADR-022 explicitly rejected that approach — the tests exercise real Lua scripts (`token_bucket.py`) and real SQL, which fakes don't faithfully reproduce.
- Don't point the service containers' host ports anywhere other than `5433`/`6379`. Reason: `tests/conftest.py`'s `os.environ.setdefault` hardcodes these exact ports as its defaults — using different ports would require also overriding env vars in the workflow for no benefit, and would diverge from the port convention `deploy/docker-compose.yml` already established for local dev.
- Do not break existing tests.
