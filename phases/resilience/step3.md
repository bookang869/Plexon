# Step 3: health-check-loop

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 3 ("Background health checks every 30s per provider-model combination; status (healthy/degraded/down) and rolling error-rate/P99 latency tracked; history persisted for post-incident analysis")
- `/docs/TRD.md` — §4.2 (`health:{provider}:{model}:status` Redis key, "30s+ (refreshed by health-check loop)"), §4.1 (`provider_health_history` table — `provider`, `model` (nullable), `status`, `error_rate`, `p99_latency_ms`, `created_at`), §5 (`health_check.interval_seconds` config)
- `/docs/ADR.md` — ADR-025 (mock fault injection is stateless and per-request via `X-Mock-Fault`/model-suffix — read this carefully, it's the reason this step's loop is *not* wired into the circuit breaker; see the design note below)
- `gateway/providers/base.py` — `HealthStatus` (`provider`, `healthy`, `latency_ms`, `error`) and the `ProviderAdapter.health_check()` protocol method — note it takes no model argument and returns no per-model breakdown
- `gateway/config/loader.py` — `start_config_watcher`/`_watch_loop` pattern (an `asyncio.create_task`-based background loop wired into `main.py`'s lifespan). Mirror this shape for the health-check loop; don't invent a different background-task pattern.
- `gateway/main.py` — the `lifespan` context manager you'll wire this into
- `gateway/db.py`, `gateway/redis_client.py` — `get_pool()`, `get_redis()`
- `phases/resilience/step0.md`'s actual output: `gateway/resilience/circuit_breaker.py` — read it to confirm what it does and does *not* import; this step must not create any dependency between the two files in either direction

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

**Design decision already made (don't re-litigate): this loop is fully independent of the circuit breaker.** It never opens/closes a breaker, and the breaker never reads anything this loop writes. Two reasons: (1) ADR-025's mock fault injection is triggered per-request via a header/model-suffix that a generic health-check ping never carries, so during a simulated outage the health-check loop would report the mock as healthy the whole time even while real traffic is genuinely failing — wiring the breaker to this loop's results would make the breaker either fail to open when it should, or close prematurely, directly undermining the demo. (2) Two independent writers racing to mutate the same breaker state (real request path + this background task) is unnecessary nondeterminism. This loop exists purely to populate the Operations dashboard and `provider_health_history` — build it in `gateway/resilience/health_check.py` with zero imports from `circuit_breaker.py` and vice versa.

**Design decision already made: provider-level pings, not true per-model checks.** `ProviderAdapter.health_check()` takes no model argument (see `gateway/providers/base.py`) — it's a single ping per provider, not per model, and none of the three adapters' `health_check()` implementations differentiate by model. Rather than changing the `ProviderAdapter` protocol (a cross-cutting change touching all three adapters, for a distinction the mocks couldn't meaningfully honor anyway per the note above), this step pings once per provider per tick and publishes that same result to `health:{provider}:{model}:status` for every model that provider serves (from `config.providers.<name>.models`) — satisfying TRD §4.2's documented key pattern literally, with the known simplification that all of a provider's models currently share one status. `provider_health_history` rows are written once per provider per tick with `model` left `NULL` (the column is nullable for exactly this reason).

### 1. Rolling health window + status computation — `gateway/resilience/health_check.py`

```python
class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"

WINDOW_SIZE = 5
LATENCY_DEGRADED_THRESHOLD_MS = 2000

async def check_provider_health(
    redis: Redis, pool: asyncpg.Pool, provider: str, adapter: ProviderAdapter, config: GatewayConfig,
) -> HealthState:
    """One tick for one provider: calls adapter.health_check(), records the
    result into a rolling window of the last WINDOW_SIZE pings (Redis list,
    LPUSH + LTRIM), computes the resulting HealthState, publishes it to
    health:{provider}:{model}:status for every model in
    config.providers.<provider>.models, and writes one provider_health_history
    row. Returns the computed state (useful for tests)."""
```

**Rolling window storage.** A Redis list at `health:{provider}:recent` (separate key from the published per-model status strings) holding the last `WINDOW_SIZE` ping outcomes, most recent first (`LPUSH` then `LTRIM 0 WINDOW_SIZE-1`). Store enough per entry to compute status: whether it succeeded and its latency — e.g. `json.dumps({"healthy": bool, "latency_ms": float | None})` per list entry is simplest.

**Status computation, in order:**
1. Most recent ping failed (`healthy=False`) → `DOWN`.
2. Most recent ping succeeded, but the window has fewer than `WINDOW_SIZE` entries yet, or `latency_ms > LATENCY_DEGRADED_THRESHOLD_MS`, or any *other* entry in the window failed → `DEGRADED`.
3. All `WINDOW_SIZE` entries succeeded and the most recent latency is under threshold → `HEALTHY`.

(Fewer than `WINDOW_SIZE` entries — e.g. right after the gateway starts — counting as `DEGRADED` rather than `HEALTHY` is deliberate: don't report full confidence before there's actually a full window of evidence.)

**Publishing.** Write `health:{provider}:{model}:status` (plain string, one of `healthy`/`degraded`/`down`) for every model in that provider's configured `models` list, with a TTL comfortably longer than `health_check.interval_seconds` (e.g. `interval_seconds * 3`) so a stalled/crashed loop causes the key to expire and go stale-absent rather than lying forever about a provider being healthy.

**Postgres history write.** One `provider_health_history` row per provider per tick (`provider`, `model=NULL`, `status`, `error_rate` — fraction of the window that failed, e.g. `1/5` → `0.2` — `p99_latency_ms` — for a 5-sample window a true P99 isn't meaningful; just use the most recent successful ping's `latency_ms`, or `NULL` if the most recent ping failed). Mirror `circuit_breaker.py`'s / `budget.py`'s best-effort pattern: log via `logger.exception` and never raise on Postgres failure — this write must never take down the loop.

### 2. The background loop

```python
async def run_health_check_loop(config: GatewayConfig) -> None:
    """Mirrors gateway/config/loader.py's _watch_loop shape: an infinite loop,
    sleeping config.health_check.interval_seconds between ticks, calling
    check_provider_health for every configured provider each tick. Catches
    and logs (doesn't propagate) any exception from a single provider's check
    so one provider's failure never stops the others from being checked or
    kills the loop."""

def start_health_check_loop(config: GatewayConfig) -> asyncio.Task:
    """asyncio.create_task(run_health_check_loop(config)) -- mirrors
    start_config_watcher()'s shape."""
```

Wire `start_health_check_loop(get_config())` into `gateway/main.py`'s `lifespan`, alongside the existing `init_pool`/`init_redis`/`start_config_watcher` calls (after `start_config_watcher`, since it needs `get_config()` to already have loaded).

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d redis postgres
uv run pytest tests/test_health_check.py -v
docker compose -f deploy/docker-compose.yml down
```

`tests/test_health_check.py` — test `check_provider_health` directly (one tick at a time), not the infinite `run_health_check_loop`, matching how `gateway/config/loader.py`'s `_watch_loop` is never directly tested either (only `reload_config()` is). Use stub `ProviderAdapter`-shaped classes (same convention as steps 1/2) with a controllable `health_check()` return value:
- A provider whose `health_check()` always returns `healthy=True` under the latency threshold, ticked `WINDOW_SIZE` times, computes `HEALTHY`.
- A provider whose most recent ping is `healthy=False` computes `DOWN`, regardless of prior ticks' results.
- A provider with fewer than `WINDOW_SIZE` ticks so far (even if all succeeded) computes `DEGRADED`.
- A provider with a full healthy window but a most-recent `latency_ms` over `LATENCY_DEGRADED_THRESHOLD_MS` computes `DEGRADED`.
- A provider with a full window where the most recent ping succeeded but one earlier entry failed computes `DEGRADED`.
- `health:{provider}:{model}:status` is set correctly for *every* model configured for that provider (use `test_config.yaml`'s multi-model `openai` entry — `gpt-4o` and `gpt-4o-mini` — and confirm both keys got written identically).
- A `provider_health_history` row is written each tick with the correct `status`/`error_rate`; a simulated Postgres failure doesn't raise out of `check_provider_health` and doesn't corrupt the Redis-side window/status writes.
- `gateway/resilience/health_check.py` has no import of `gateway/resilience/circuit_breaker.py`, and vice versa (a quick `grep`/import check is sufficient — this is enforcing the "fully independent" design decision, not just testing behavior).

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Is `health_check.py` genuinely independent of `circuit_breaker.py` — no shared Redis keys, no function calls in either direction?
   - Does a single provider's `health_check()` exception get caught and logged inside the loop, rather than crashing the whole background task?
   - Does the Redis key TTL prevent a stalled loop from leaving a permanently-stale "healthy" status?
3. Based on the result, update `phases/resilience/index.json` step 3:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created, the rolling-window/status-threshold rules, the provider-level-not-per-model simplification, confirmation this is fully decoupled from circuit_breaker.py -- marking the resilience phase complete"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't call anything in `circuit_breaker.py` from this loop, or read this loop's Redis keys/Postgres rows from anywhere in the retry/fallback/breaker code (steps 0-2). Reason: explicitly decided — these are two independent mechanisms answering different questions; conflating them would make the breaker blind to real outages during exactly the scenario (ADR-025's per-request-only fault injection) this phase's demo relies on.
- Don't add a model parameter to the `ProviderAdapter.health_check()` protocol or change any adapter's implementation. Reason: out of scope for this step — a real per-model distinction isn't meaningful given the mocks' per-request (not per-check) fault model; the provider-level-ping-published-to-every-model simplification is the deliberate, documented tradeoff here.
- Don't let a single provider's health-check failure (e.g. an exception inside `adapter.health_check()` itself, not just `healthy=False`) stop other providers from being checked that tick, or kill the loop for future ticks.
- Do not break existing tests.
