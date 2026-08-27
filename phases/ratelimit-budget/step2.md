# Step 2: budget-enforcement

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ARCHITECTURE.md` — "State Management" section (Postgres `spend_ledger` is the source of truth for spend; Redis's spend counter is fast-path only and can be rebuilt from Postgres after a restart)
- `/docs/ADR.md` — ADR-004 (spend tracking is dual-write: Redis fast-path counter + Postgres ledger as source of truth), ADR-018 (per-model $/token pricing lives in YAML, real published list prices, applies uniformly to mocked and real providers)
- `/docs/TRD.md` — §4.1 (`spend_ledger` table schema — already exists, unused until now), §4.2 (`spend:{team_id}:{period}` Redis key pattern), §5 (YAML `pricing` section shape, already loaded)
- `/docs/PRD.md` — Core Feature 2 ("Per-team monthly/daily dollar budgets, computed from `input tokens × input price + output tokens × output price`. Warning at 80% utilization; hard block at 100% with a clear error.")
- `phases/ratelimit-budget/step1.md`'s actual output: `gateway/ratelimit/limiter.py` and its wiring into `gateway/routes.py` (the rate-limit check now runs before this step's budget check — TRD §3 orders budget check at step 4, right after rate-limit's step 3)
- `gateway/config/loader.py` — `GatewayConfig.pricing: PricingConfig` (`dict[str, ModelPricing]` per provider, `ModelPricing.input_per_1k`/`output_per_1k`)
- `gateway/auth/team_auth.py` — `Team.daily_budget_usd`/`monthly_budget_usd` (both `Decimal | None` — a team with no budget configured on either field means unlimited on that dimension, don't treat `None` as zero)
- `gateway/schemas.py` — `Usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`)
- `gateway/redis_client.py` (from step 0) — `get_redis()`
- `deploy/schema.sql` — `spend_ledger` table columns exactly (`team_id`, `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `request_id`, `created_at`)

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

Add dollar-budget tracking and enforcement, separate from step 1's rate-limit buckets (this is a running counter compared against a ceiling, not a refilling bucket — don't reuse `token_bucket.py`'s primitive here, it solves a different problem).

### `gateway/ratelimit/budget.py`

```python
def compute_cost(usage: Usage, provider: str, model: str, pricing: PricingConfig) -> Decimal:
    """cost = (usage.prompt_tokens / 1000) * input_per_1k + (usage.completion_tokens / 1000) * output_per_1k,
    looked up from pricing[provider][model]. Raise a clear error if the provider/model
    combination has no pricing entry -- don't silently default to zero cost."""

class BudgetStatus(BaseModel):
    blocked: bool          # True if either daily or monthly is at/over 100%
    warning: bool          # True if either daily or monthly is at/over 80% (and not blocked)
    daily_utilization: float | None    # None if team has no daily_budget_usd configured
    monthly_utilization: float | None  # None if team has no monthly_budget_usd configured

async def check_budget(team: Team) -> BudgetStatus:
    """Reads current daily/monthly spend from Redis counters (see key scheme below),
    compares against team.daily_budget_usd/monthly_budget_usd. A team with both
    fields None is never blocked or warned."""

async def record_spend(
    team: Team, provider: str, model: str, usage: Usage, cost: Decimal, request_id: str,
) -> None:
    """Dual-write: INCRBYFLOAT the Redis daily+monthly counters (fast path), and
    INSERT a row into spend_ledger (source of truth, ADR-004). Do both even if one
    of the team's budget fields is None -- the ledger is the durable record regardless
    of whether a budget is actively enforced."""
```

**Redis key scheme.** TRD §4.2 gives the pattern `spend:{team_id}:{period}` — use two concrete keys per team: `spend:{team_id}:daily:{YYYY-MM-DD}` and `spend:{team_id}:monthly:{YYYY-MM}` (UTC dates), each with a Redis `EXPIRE` set past that period's natural end (e.g. daily key TTL'd ~2 days out, monthly ~32 days out) so old period counters don't accumulate forever — the *ledger* is where historical data lives, per ADR-004; Redis counters are explicitly disposable/rebuildable.

**Core rule — never let Postgres write failure silently lose spend data, but never let it block the response either.** `record_spend` runs after the provider has already produced a response — the caller is getting their answer regardless. If the Postgres insert fails, don't raise an exception that would turn a successful LLM call into a 500 to the client; log the failure loudly (this is a real gap between Redis's fast counter and the durable ledger, worth a `logger.exception` at minimum) and let the Redis-side dual-write stand. Do not add retry/queueing infrastructure for this — out of scope for this step's rigor level.

### Wiring into `gateway/routes.py`

Right after step 1's rate-limit check (before `_prepare_request`'s enrichment logic, matching TRD §3's step 4 ordering), call `check_budget(team)`. If `status.blocked`, raise `HTTPException(status_code=402, detail=...)` — deliberately distinct from rate-limiting's `429` so callers/tests can tell "you're out of budget" apart from "you're sending too fast." If `status.warning` (and not blocked), don't block the request — instead attach a response header `X-Budget-Warning: true` (for both streaming and non-streaming responses).

After a successful response (same two hook points as step 1's `reconcile_tpm` — non-streaming return value, and streaming's `on_complete` callback), compute the cost via `compute_cost` and call `record_spend`. Use the response's own `usage` and `model` fields (the model actually served, which may differ from what was requested if a later phase adds fallback — for now they're the same, but read from the response, not the request, to be forward-consistent with TRD §3 step 8's "response translation" already having happened by this point) and the resolved provider name (available from whichever adapter/registry lookup `_prepare_request` already performed — don't re-resolve it).

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build redis postgres mock-openai mock-anthropic
uv run pytest tests/test_budget.py tests/test_routing.py tests/test_streaming.py tests/test_priority_tiers.py -v
docker compose -f deploy/docker-compose.yml down
```

`tests/test_budget.py` must cover, using the `seeded_team` fixture and real mocks/Redis/Postgres:
- A request from a team well under budget succeeds and a `spend_ledger` row is written with the correct `cost_usd` (verify the arithmetic against `config.yaml`'s pricing table for the model used).
- A team seeded with a `daily_budget_usd` already exhausted (insert spend directly, or send enough requests) gets `402` on the next request, and no additional `spend_ledger` row is written for the blocked request.
- A team at ≥80% but <100% utilization gets a successful response with `X-Budget-Warning: true` in the response headers.
- A team with `daily_budget_usd`/`monthly_budget_usd` both `None` is never blocked or warned regardless of usage.
- `compute_cost` raises a clear error for a provider/model combination absent from `config.yaml`'s `pricing` section (don't silently charge $0).

## Verification Procedure

1. Run the AC commands above, and confirm `tests/test_routing.py`/`tests/test_streaming.py`/`tests/test_priority_tiers.py` still pass (this step adds a new check between rate-limiting and enrichment — easy to accidentally break the existing ordering).
2. Check the architecture checklist:
   - Is `spend_ledger` genuinely the source of truth, with Redis only a fast-path cache (ADR-004)? (I.e., could you delete the Redis counters and reconstruct daily/monthly spend from `spend_ledger` alone?)
   - Is budget enforcement's `402` distinguishable from rate-limiting's `429` in both status code and by a test?
   - Does pricing come only from YAML (ADR-018), with no hardcoded per-model $ figures in `budget.py`?
3. Based on the result, update `phases/ratelimit-budget/index.json` step 2:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files/functions added, Redis key scheme used, status codes for block/warning, where spend recording is hooked into streaming vs non-streaming"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't reuse `gateway/ratelimit/token_bucket.py`'s refill/consume primitive for spend tracking. Reason: budget is a running counter against a ceiling, not a refilling bucket — different semantics, forcing it into the bucket abstraction would be a worse fit, not code reuse.
- Don't let a Postgres write failure in `record_spend` turn a successful LLM response into an error for the caller. Reason: the provider already answered; the client shouldn't be punished for an internal logging failure.
- Don't add retry/queue infrastructure around the Postgres ledger write. Reason: out of scope for this project's rigor level (ADR-002) — log and move on.
- Don't treat a `None` budget field as a zero budget (i.e. don't block a team that has no budget configured at all). Reason: `Team.daily_budget_usd`/`monthly_budget_usd` being `None` means "no cap on this dimension," per the existing schema/model.
- Do not break existing tests, especially `tests/test_routing.py`, `tests/test_streaming.py`, and `tests/test_priority_tiers.py`.
