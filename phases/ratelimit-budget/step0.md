# Step 0: rate-limiter-core

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ARCHITECTURE.md` — "State Management" section (Redis is hot-path only, never system of record)
- `/docs/ADR.md` — ADR-004 (Redis vs Postgres split), ADR-007 (nothing important lives only in process memory — everything through Redis), ADR-011 (tiered rate limiting reuses "the existing Redis token-bucket mechanism")
- `/docs/TRD.md` — §4.2 (Redis Key Schema: `ratelimit:{team_id}:{tier}:rpm` / `:tpm` key patterns), §10 (non-functional requirement: rate-limit accuracy under 5,000+ concurrent requests)
- `/docs/PRD.md` — Core Feature 2 ("Per-team token bucket rate limiting ... enforced atomically via Redis")
- `gateway/db.py` — the existing Postgres pool pattern (`init_pool`/`close_pool`/`get_pool`, module-level singleton, `RuntimeError` if used before init). Mirror this exact shape for Redis.
- `gateway/main.py` — the FastAPI `lifespan` context manager where `init_pool()`/`close_pool()` are wired in. You'll add the Redis equivalents here.

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

This step builds a **generic, team/tier-agnostic** atomic token-bucket primitive against Redis. It has no knowledge of teams, priority tiers, or HTTP — that orchestration is step 1's job. This step is done when the primitive itself is correct and independently tested.

### 1. Redis connection module — `gateway/redis_client.py`

Mirror `gateway/db.py`'s exact pattern using `redis.asyncio.Redis` (the `redis` package is already a dependency in `pyproject.toml`):

```python
async def init_redis() -> None: ...
async def close_redis() -> None: ...
def get_redis() -> redis.asyncio.Redis: ...
```

- DSN from `os.environ["PLEXON_REDIS_URL"]` (required, no default — same strictness as `PLEXON_DATABASE_URL` in `db.py`).
- Wire `init_redis()`/`close_redis()` into `gateway/main.py`'s `lifespan`, alongside the existing Postgres calls.
- Add `PLEXON_REDIS_URL: "redis://redis:6379/0"` to the `gateway` service's `environment` block in `deploy/docker-compose.yml` (it's currently missing — the gateway container has no way to reach Redis today).

### 2. Token bucket primitive — `gateway/ratelimit/token_bucket.py`

A continuous-refill token bucket (not a fixed-window counter, not a sliding-window log — those were considered and rejected: fixed windows allow a 2x boundary-burst which conflicts with the "verified accurate under load" success metric, and sliding-window logs don't generalize to variable-cost consumption, which this bucket needs for token-per-minute limiting in step 1). Storage: a Redis hash per bucket key with fields `tokens` and `ts` (last-refill timestamp).

```python
class BucketResult(BaseModel):
    allowed: bool
    remaining: float
    retry_after_seconds: float | None  # None when allowed=True


async def check_and_consume(
    redis: Redis, key: str, capacity: float, refill_per_second: float,
    cost: float = 1.0, ttl_seconds: int = 120,
) -> BucketResult: ...


async def refund(redis: Redis, key: str, capacity: float, amount: float) -> None: ...
```

**Core rule — must be a single atomic Redis operation.** `check_and_consume` must read the current bucket state, compute refill, check capacity, and deduct — all in one round trip with no other client able to interleave. Implement this as a Lua script run via `EVAL`/`register_script` (not `WATCH`/`MULTI`, not separate `GET`+`SET` calls — those have a TOCTOU gap under concurrent callers hitting the same key, which is exactly the scenario the 5,000-concurrent-request success metric will exercise).

Use Redis's own `TIME` command *inside* the Lua script for the current timestamp (not a timestamp passed in from Python) — this avoids clock skew between the app process and Redis, and keeps the whole read-compute-write sequence deterministic within the script.

Bucket refill logic: on first access (key doesn't exist), initialize `tokens = capacity`. Otherwise `tokens = min(capacity, tokens + elapsed_seconds * refill_per_second)`. If `tokens >= cost`, deduct and allow; otherwise deny and compute `retry_after_seconds = (cost - tokens) / refill_per_second`. Always write back the updated `tokens`/`ts` and refresh the key's TTL (so idle buckets don't linger in Redis forever — `ttl_seconds` should comfortably exceed the time a bucket could need to fully refill from empty, so a legitimately-idle-then-returning caller doesn't get treated as brand new mid-window in a way that changes behavior versus if the key had simply persisted).

`refund` is for step 1's use (crediting back an over-estimated token cost after the real response is known) — it should add `amount` back to the bucket, capped at `capacity`, without needing to know refill rate (no time-based computation, just a bounded add).

Lua returns non-float-preserving integers by default for large numbers — return the numeric fields as strings from the script (`tostring(...)`) and parse them back to `float` in Python, or you'll silently truncate fractional token counts.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d redis
uv run pytest tests/test_token_bucket.py -v
docker compose -f deploy/docker-compose.yml down
```

`tests/test_token_bucket.py` (add `PLEXON_REDIS_URL` default to `tests/conftest.py`, e.g. `redis://localhost:6379/0`, matching how `PLEXON_DATABASE_URL` already defaults there for local runs against the docker-compose port mapping) must cover, against a real Redis instance:
- A bucket admits requests up to `capacity`, then denies the next one with `retry_after_seconds > 0`.
- After waiting long enough for the configured refill rate to replenish at least `cost` tokens (use a small capacity/fast refill rate in the test so this doesn't require a real sleep of more than a second or two), a previously-denied request is now admitted.
- **Concurrency correctness**: fire `capacity + N` concurrent `check_and_consume` calls (via `asyncio.gather`) at the same fresh key with `refill_per_second=0`; exactly `capacity` of them must report `allowed=True` and the rest `allowed=False` — no over-admission. This is the test that actually proves the "atomic" claim.
- `refund` increases `remaining` on a subsequent `check_and_consume` call, capped at `capacity` (refunding more than was ever consumed doesn't blow past the ceiling).
- Variable `cost` (e.g. `cost=50` against `capacity=100`) is deducted correctly, not treated as `cost=1`.

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Is `token_bucket.py` fully generic — no `team_id`, `tier`, or HTTP concepts anywhere in it? (That belongs in step 1.)
   - Is the check-and-deduct sequence genuinely atomic (one Lua script), not two round trips?
   - Does nothing here store rate-limit state anywhere but Redis (ADR-007)?
3. Based on the result, update `phases/ratelimit-budget/index.json` step 0:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created, the Lua script's atomicity mechanism, exact function signatures for check_and_consume/refund, so step 1 can import them directly"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed (e.g. Redis image unavailable) → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't implement team/tier-specific logic, HTTP wiring, or `X-Priority` header handling here. Reason: this step is the primitive only — step 1 owns all business/routing logic, kept in a separate file so the primitive stays independently testable and reusable.
- Don't use `WATCH`/`MULTI` or separate `GET`+`SET` calls for the check-and-deduct sequence. Reason: race-prone under concurrent access to the same key, which defeats the entire point of this step.
- Don't add a fixed-window or sliding-window-log implementation "as an alternative" or "for comparison." Reason: this was already decided (continuous-refill token bucket) — no speculative alternatives.
- Do not break existing tests.
