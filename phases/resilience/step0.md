# Step 0: circuit-breaker-core

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 3 ("Circuit breaker per provider: opens after N failures in M seconds, routes all traffic to fallbacks, half-open test request after cooldown, closes on success. Every state transition is logged and emitted as a Prometheus metric.")
- `/docs/TRD.md` — §4.2 (Redis Key Schema: `breaker:{provider}:state` / `breaker:{provider}:failures`), §4.1 (`circuit_breaker_history` table schema), §5 (`circuit_breaker` YAML config: `failure_threshold`, `window_seconds`, `cooldown_seconds`)
- `/docs/ADR.md` — ADR-004 (Redis vs Postgres split), ADR-007 (nothing important lives only in process memory — everything through Redis)
- `gateway/redis_client.py`, `gateway/db.py` — the existing connection singletons you'll use (`get_redis()`, `get_pool()`)
- `gateway/ratelimit/token_bucket.py` — the atomic-Lua-script pattern this step must follow for correctness under concurrent access. Read this file fully before writing any circuit-breaker code.
- `gateway/config/loader.py` — `CircuitBreakerConfig` (already defined: `failure_threshold: int`, `window_seconds: int`, `cooldown_seconds: int`) and `get_config()`
- `deploy/schema.sql` — `circuit_breaker_history` table (already exists, created in an earlier phase; this step writes to it, doesn't create it)

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

This step builds a **generic, provider-agnostic** circuit-breaker state machine. It has no knowledge of retry logic, fallback chains, or what makes an error "retryable" — that's step 1's job. This step doesn't know or care what kind of failure it's being told about; it just tracks state transitions correctly and atomically. Build it in `gateway/resilience/circuit_breaker.py`.

### Design decision already made (don't re-litigate)

**Circuit breaker state is Redis-backed, not in-process.** `docs/ADR.md`'s ADR-010 currently says "in-process and per-instance (not Redis-backed)" — that decision has been superseded after review. Before writing code, edit `docs/ADR.md`'s ADR-010 section: keep the existing Context/Decision/Consequences as historical record but add a new subsection immediately after it:

```markdown
**Superseded — Redis-Backed (resilience phase implementation):** The in-process design conflicts with ADR-007's CRITICAL rule (restated in CLAUDE.md) that no important state — explicitly including circuit-breaker state — may live only in a single process's memory. `docs/TRD.md`'s Redis key schema (`breaker:{provider}:state`, `breaker:{provider}:failures`) and `docs/ARCHITECTURE.md`'s State Management section already assumed Redis-backed breaker state, so the in-process choice above was inconsistent with the rest of the design even before this reversal. Circuit breaker state now lives in Redis (this file), atomically transitioned via Lua scripts (same pattern as `gateway/ratelimit/token_bucket.py`), consistent with every other piece of hot-path state in the system.
```

### Redis key schema (extends TRD §4.2 — the two keys documented there aren't sufficient to implement cooldown/half-open correctly)

- `breaker:{provider}:state` — string, one of `closed` / `open` / `half_open`. No TTL (explicit transitions only, per TRD).
- `breaker:{provider}:failures` — counter, TTL = `window_seconds`. Incremented on each reported failure while closed; naturally expires so failures outside the window stop counting.
- `breaker:{provider}:opened_at` — the Redis `TIME`-sourced timestamp of the most recent `closed→open` transition, used to compute whether `cooldown_seconds` has elapsed. Not in the original TRD table; needed to make the open→half-open transition correct without polling/wall-clock drift between the app process and Redis.
- `breaker:{provider}:probe_claimed` — short-lived marker (TTL comfortably longer than a single provider call could plausibly take, e.g. 60s, so an abandoned probe doesn't permanently wedge the breaker in half-open) set atomically by whichever caller wins the half-open probe slot. Cleared when the probe resolves (via `record_success`/`record_failure`).

### Public API

```python
class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BreakerDecision(BaseModel):
    allowed: bool   # False means: don't call this provider at all right now
    is_probe: bool  # True means: this specific call is the half-open probe -- its
                     # outcome (via record_success/record_failure) decides whether
                     # the breaker closes or reopens, unlike a normal closed-state call


async def check_breaker(redis: Redis, provider: str, config: CircuitBreakerConfig) -> BreakerDecision: ...

async def record_success(redis: Redis, provider: str, was_probe: bool) -> None: ...

async def record_failure(redis: Redis, provider: str, was_probe: bool, config: CircuitBreakerConfig) -> None: ...
```

**Core rules — must not be violated:**

1. **`check_breaker` must be a single atomic Redis operation** (Lua `EVAL`/`register_script`, using Redis `TIME` internally for the elapsed-cooldown check — same reasoning as `token_bucket.py`: no TOCTOU gap under concurrent callers, no clock skew between app and Redis).
2. **Exactly one concurrent caller may win the half-open probe.** When `check_breaker` observes `state=open` and the cooldown has elapsed, it must atomically: transition state to `half_open`, set `probe_claimed`, and return `allowed=True, is_probe=True` to exactly one caller. Every other concurrent caller — whether they arrive microseconds before, during, or after that transition — must see `allowed=False` (treated as if still open) until the probe resolves. This is the entire point of the Lua-script requirement: a naive read-then-write would let many concurrent callers all observe "cooldown elapsed" and all become probes.
3. **A probe's outcome is decisive, independent of `failure_threshold`.** `record_failure(..., was_probe=True, ...)` must immediately transition `half_open → open` (reset `opened_at` to now, clear `probe_claimed`) — it does not wait to accumulate `failure_threshold` failures again; one bad probe is enough. `record_success(..., was_probe=True)` must immediately transition `half_open → closed` (clear `failures` and `probe_claimed`).
4. **A normal (non-probe) failure only opens the breaker at the threshold.** `record_failure(..., was_probe=False, ...)` increments `breaker:{provider}:failures` (refreshing its TTL to `window_seconds`); only when the incremented count reaches `failure_threshold` does it transition `closed → open` (set `opened_at` to now). Below threshold, state stays `closed`.
5. **Every actual state transition** (`closed→open`, `open→half_open`, `half_open→closed`, `half_open→open`) **writes one `circuit_breaker_history` row** (`provider`, `from_state`, `to_state`, `reason` — e.g. `"failure_threshold_reached"`, `"cooldown_elapsed"`, `"probe_succeeded"`, `"probe_failed"` — `created_at` defaults). Use `gateway/db.py`'s `get_pool()`. Mirror `gateway/ratelimit/budget.py`'s `record_spend` pattern: this write is best-effort — log via `logger.exception` on failure, never raise, never block the breaker decision on Postgres being reachable (ADR-004: Postgres is durable history, not the hot-path system of record).
6. **This module has no concept of "retryable" vs "non-retryable" errors.** It doesn't import from `gateway/providers/errors.py`. Callers (step 1) decide what counts as a failure worth reporting; this module just records what it's told.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d redis postgres
uv run pytest tests/test_circuit_breaker.py -v
docker compose -f deploy/docker-compose.yml down
```

`tests/test_circuit_breaker.py` must cover, against real Redis and Postgres:
- Closed state: `check_breaker` returns `allowed=True, is_probe=False`.
- `record_failure` below `failure_threshold` keeps the breaker closed.
- `record_failure` reaching `failure_threshold` within `window_seconds` opens the breaker (`check_breaker` now returns `allowed=False`), and writes a `closed→open` `circuit_breaker_history` row.
- While open and before `cooldown_seconds` has elapsed, `check_breaker` returns `allowed=False`.
- After `cooldown_seconds` elapses (use a short `cooldown_seconds` in the test config so this doesn't require a real multi-second sleep), `check_breaker` transitions to half-open and returns `allowed=True, is_probe=True` — and a `circuit_breaker_history` row records `open→half_open`.
- **Concurrency correctness**: once cooldown has elapsed, fire many concurrent `check_breaker` calls (`asyncio.gather`) at the same provider key — exactly one must get `is_probe=True`; the rest must get `allowed=False`. This is the test that actually proves the single-winner-probe claim.
- `record_success(was_probe=True)` closes the breaker; a subsequent `check_breaker` returns `allowed=True, is_probe=False` (normal closed traffic, not probe traffic) and `failures` has been reset (verify by driving it toward threshold again from zero).
- `record_failure(was_probe=True, ...)` reopens the breaker immediately (not waiting for `failure_threshold`), and resets `opened_at` so cooldown timing restarts from that failure.
- Postgres write failures during `record_failure`/`record_success` (e.g. simulate by closing the pool, or by another means that reliably breaks the Postgres write) don't raise out of the function and don't corrupt the Redis-side state transition.

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Is `circuit_breaker.py` fully generic — no imports from `gateway/providers/errors.py`, no concept of "retryable," no HTTP/adapter knowledge anywhere in it? (That belongs in step 1.)
   - Is every state transition (`check_breaker`'s open→half-open, and both `record_*` functions' transitions) driven by a single atomic Redis operation, not multiple round trips?
   - Does `docs/ADR.md`'s ADR-010 now clearly document the Redis-backed reversal, without silently deleting the original historical decision?
   - Is the Postgres write for `circuit_breaker_history` genuinely non-blocking (breaker decisions never fail or hang because Postgres is unreachable)?
3. Based on the result, update `phases/resilience/index.json` step 0:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created, exact function signatures for check_breaker/record_success/record_failure, the Redis key schema (including opened_at/probe_claimed additions beyond TRD §4.2), and the ADR-010 amendment, so step 1 can import and wire this in directly"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't give this module any knowledge of retry logic, fallback chains, or which HTTP status codes/exception types count as failures. Reason: step 1 owns that classification; this step is the state-machine primitive only, kept independently testable exactly like `token_bucket.py` is independent of `limiter.py`.
- Don't implement the breaker as in-process/per-instance state, and don't leave ADR-010 reading as if that's still the decision. Reason: this was explicitly re-decided — Redis-backed, with the ADR amended to record why.
- Don't let multiple concurrent requests all become half-open probes. Reason: this defeats the entire purpose of the half-open state (cautiously testing recovery with one canary request, not fully reopening traffic while still unsure) and was explicitly designed against.
- Don't make the `circuit_breaker_history` write block or fail the breaker decision if Postgres is down. Reason: ADR-004 — Postgres is durable history, Redis is the hot-path system of record; a history-logging failure must never take down request routing.
- Don't add a Prometheus metric emission here even though the PRD mentions one ("emitted as a Prometheus metric"). Reason: Prometheus/OTel instrumentation is the `observability` phase's job (phase 3 of 4 remaining); `gateway/observability/` doesn't exist yet beyond an empty package.
- Do not break existing tests.
