# Step 1: integration-ratelimit-budget

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 5 ("Integration tests: rate limiting under concurrent load, budget cap enforcement... using mocked providers with fault injection.")
- `/docs/TRD.md` — §4.2 (Redis key convention `ratelimit:{team_id}:{tier}:{rpm,tpm}`, `spend:{team_id}:{daily,monthly}:{period}`), §10 (Non-Functional Requirements: "Rate-limit accuracy: correct under concurrent load (no over/under-admission)")
- `/docs/ADR.md` — ADR-002 (the rpm/tpm dual-bucket check is deliberately not one atomic two-key script — a small race window between the two is an accepted tradeoff, don't "fix" it here), ADR-022 (real Redis/Postgres for integration tests, not fakes)
- `CLAUDE.md` (project root) — "New resilience/rate-limit logic must be covered by the integration test suite (concurrent-load rate limiting, budget enforcement...)"
- `gateway/ratelimit/token_bucket.py` — `check_and_consume`/`refund`, read the Lua script's comment carefully: it's a single `EVAL`, so concurrent callers against the *same key* cannot interleave read-compute-write. This is what makes a strict, exact-count concurrency assertion valid for the rpm dimension.
- `gateway/ratelimit/limiter.py` — `check_rate_limit`'s docstring: the rpm-then-tpm two-key check has a "brief window... an accepted tradeoff for this project's scope (ADR-002)" — read this before writing any test that assumes the *combined* rpm+tpm check is perfectly atomic; it isn't, by design.
- `gateway/ratelimit/budget.py` — `check_budget`/`record_spend`. **Read this closely**: `check_budget` reads Redis once, at the start of the request, *before* the provider call; `record_spend` increments Redis *after* the provider call completes. This is a genuine check-then-act race, not a bug — under enough concurrent requests all starting while spend is just under the cap, all of them can pass `check_budget` before any of them writes. Your concurrent budget test must assert the property that actually holds (see Task below), not "spend can never exceed the cap under concurrency," which this design does not guarantee and this step is not scoped to fix.
- `tests/conftest.py` — `db_pool`/`redis_client`/`seeded_team` fixtures you'll reuse; note `seeded_team`'s fixed `rpm_limit=60`/`tpm_limit=10000` — too high for a fast, deterministic concurrency test, so you'll insert your own teams with small limits instead (same technique as `tests/test_budget.py`'s local `_insert_team`, not `seeded_team`).
- `tests/test_priority_tiers.py` — the existing `client` fixture pattern (`httpx.ASGITransport(app=app)`) and its `_insert_team(db_pool, *, rpm_limit, tpm_limit)` helper — this step's `tests/integration/test_concurrent_ratelimit_budget.py` follows the same conventions, just driving requests concurrently via `asyncio.gather` instead of sequentially.
- `tests/test_budget.py` — the local `_insert_team(db_pool, *, daily_budget_usd, monthly_budget_usd)` / `_delete_team` helpers and `compute_cost`/pricing usage — mirror this for your own team-insertion helper.
- `tests/fixtures/test_config.yaml` — the real pricing/model config your requests run against (`gpt-4o-mini`: input 0.00015/1k, output 0.0006/1k tokens — useful for sizing a budget tight enough to hit in a handful of requests).

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

Create `tests/integration/test_concurrent_ratelimit_budget.py`. This is the first real content in `tests/integration/` (currently just `__init__.py`) — it runs against the real FastAPI app (in-process, `httpx.ASGITransport`, same convention as every other route-level test file), real Redis, real Postgres, and the real `mock-openai`/`mock-anthropic` containers, driving genuinely concurrent HTTP requests via `asyncio.gather`.

### 1. Local fixtures

Add a module-local `client` fixture (identical pattern to `tests/test_priority_tiers.py`/`tests/test_budget.py`) and your own `_insert_team`/`_delete_team` helpers parameterized over whatever each test needs (small `rpm_limit`, small `daily_budget_usd`, etc.) — don't reuse the shared `seeded_team` fixture, its fixed limits are too generous for a fast deterministic concurrency test.

### 2. Concurrent rate-limit accuracy — rpm dimension

Insert a team with a small `rpm_limit` (e.g. `10`) and a **large** `tpm_limit` (e.g. `1_000_000`, so the tpm bucket is never the bottleneck and this test isolates the rpm dimension cleanly — the rpm+tpm combined check has an accepted small race window per ADR-002, but a single bucket's own `check_and_consume` is a single atomic Redis `EVAL` and can be asserted exactly). Fire `N` concurrent requests (`N` meaningfully larger than the limit, e.g. `30`) via `asyncio.gather(*[client.post(...) for _ in range(30)])` against `/v1/chat/completions` with the `realtime` tier (default, no `X-Priority` header — `rpm_ceiling_pct: 100` in `test_config.yaml` means the tier ceiling equals the raw limit). Assert **exactly** `rpm_limit` requests return `200` and the rest return `429` — no more, no fewer. This is the "no over/under-admission" property TRD §10 asks for.

### 3. Concurrent rate-limit accuracy — tpm dimension

Same shape, but with a large `rpm_limit` and a small `tpm_limit` sized so only a handful of requests fit (each request's `estimate_tokens` cost is prompt word count + `max_tokens` or the 1024-token default — set an explicit small `max_tokens` on the request body so the per-request cost is small and predictable, and size `tpm_limit` to admit an exact known count, e.g. `tpm_limit` = 3× a request's estimated cost admits exactly 3). Assert exactly that count of `200`s.

### 4. Concurrent budget enforcement — eventual consistency, not per-request atomicity

Given the check-then-act race documented above, the *correct* testable property is: once a wave of concurrent requests has been given time to complete (so their `record_spend` writes have landed), the recorded spend accurately reflects what happened, **and** a *second* wave of concurrent requests fired after the first wave has settled is uniformly rejected with `402` once the tracked spend is at or over the cap. Concretely:
- Insert a team with a small `daily_budget_usd` (e.g. `"0.01"`) and `allowed_models=["gpt-4o-mini"]`.
- Fire a first wave of `M` concurrent requests (`M` small, e.g. `5`) — some may succeed (200) and some may 402, depending on how the race resolves; don't assert an exact split here, that's the point.
- After `asyncio.gather` returns (all writes landed), query `spend_ledger` directly (`SELECT SUM(cost_usd) ...`) and assert it's `>= 0` (sanity) — the real assertion is on the *next* wave.
- Fire a second wave of `K` concurrent requests. Assert **every one** of them is `402` (spend is now over budget, and `check_budget`'s read happens fresh for each of these — no in-flight race, since the first wave has fully settled).
- This proves budget enforcement is *eventually* airtight (nothing gets through once the tracked spend has caught up), which is the real guarantee this design provides — don't write a test asserting the first wave is perfectly bounded, it isn't by design (see `budget.py`'s docstring).

### 5. Streaming path, briefly

Add one concurrent test firing several simultaneous *streaming* requests (`client.stream(...)`) against a team with a small `rpm_limit`, asserting the same exact-admission-count property as step 2 holds for the streaming branch too (rate-limiting is checked before the streaming/non-streaming fork in `routes.py`, so this should already work — this test proves it, it doesn't require new gateway code).

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d redis postgres mock-openai mock-anthropic
uv run pytest tests/integration/test_concurrent_ratelimit_budget.py -v
uv run pytest -v   # full existing suite must still pass unchanged
docker compose -f deploy/docker-compose.yml down
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does the rpm-dimension test assert an *exact* admitted count (not "at least"/"roughly")?
   - Does the budget test avoid asserting the first (racy) wave's outcome exactly, and instead assert the second (settled) wave is uniformly rejected?
   - Are new teams inserted/torn down via local helpers, not by mutating the shared `seeded_team` fixture other test files depend on?
3. Based on the result, update `phases/test-load/index.json` step 1:
   - Success → `"status": "completed"`, `"summary": "one-line summary — file created, which concurrency properties are asserted exactly vs. eventually, team-insertion helper shape"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't assert that concurrent budget checks never overshoot the cap on the first wave. Reason: `check_budget` reads before `record_spend` writes — this is a real, accepted check-then-act race (not something this step is scoped to fix), so an exact-bound assertion on a single concurrent wave will be flaky by construction, not because of a real bug.
- Don't "fix" the rpm+tpm dual-check race noted in `limiter.py`'s docstring (ADR-002) by merging it into one script or adding locking. Reason: explicitly an accepted tradeoff already decided in a prior phase; out of scope here, and isolating the rpm and tpm dimensions into separate tests (per Task §2/§3) is how to get a clean, non-flaky signal without touching that code.
- Don't reuse `tests/conftest.py`'s shared `seeded_team` fixture for these tests. Reason: its `rpm_limit=60`/`tpm_limit=10000` are sized for other tests' happy-path needs, not for a fast, small-N concurrency test — use local team-insertion helpers with small, test-specific limits instead.
- Do not break existing tests.
