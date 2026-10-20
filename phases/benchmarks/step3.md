# Step 3: failover-benchmark

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/TRD.md` — §10 (circuit breaker "opens/closes correctly under injected faults"), CLAUDE.md's CRITICAL retry rule (retry primary up to 3x for retryable errors, fall back immediately on non-retryable)
- `/docs/ADR.md` — ADR-025 (mock fault injection: stateless, per-request, magic model-name suffix — no shared toggle exists, which is why this step drives recovery a specific way, see below)
- `phases/benchmarks/step0.md`'s actual output — `benchmarks/common.py`, `benchmarks/conftest.py`'s `benchmark_team`/`db_pool`, `benchmarks/thresholds.py`'s failover/recovery constants
- `gateway/providers/errors.py` — `raise_for_provider_status`: confirms both a 429 and a 500 classify as `RetryableProviderError` (only 400/401/403 are non-retryable) — this is why this benchmark needs only the existing `--fault-error` (500) magic model, not a separate `--fault-rate_limit` one, to exercise the retry→fallback→breaker path
- `gateway/resilience/circuit_breaker.py` — `_state_key`/`_failures_key`/`_opened_at_key`/`_probe_claimed_key`, and the fact that breaker state is keyed **only by provider name**, not provider+model. This is the key fact enabling real close-recovery measurement below: any successful call to `anthropic`, even via a *different* model than the one that opened the breaker, satisfies the half-open probe.
- `gateway/resilience/orchestrator.py` — `_PRIMARY_MAX_ATTEMPTS = 3`, `_RETRY_WAIT_MULTIPLIER_SECONDS = 0.1`, `_RETRY_WAIT_MAX_SECONDS = 2.0` — bounds on how long a single fallback-triggering request can take
- `config.yaml` (project root) — `circuit_breaker.cooldown_seconds` (30), `providers.anthropic.models` (includes `claude-sonnet--fault-error` and plain `claude-sonnet`, both already configured against the real `anthropic` provider by the test-load phase)
- `tests/integration/test_concurrent_resilience.py` — read this file in full; it's the closest existing precedent (concurrent fallback activation, `anthropic_breaker`-style Redis+Postgres reset fixture, `circuit_breaker_history` row assertions) but it never demonstrates a `half_open -> closed` transition, because every request in it uses the always-failing model. This benchmark's recovery test is the first thing in this codebase to do so.
- `deploy/schema.sql` — `circuit_breaker_history` columns (`provider, from_state, to_state, reason, created_at`) — used to compute recovery latency from real timestamps

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

Create `benchmarks/bench_failover.py`, running against the real docker-compose stack (real `anthropic` provider name, real breaker state in Redis/Postgres — same shared-state caution `test_concurrent_resilience.py`'s module docstring calls out). Add a fixture resetting `anthropic`'s breaker (Redis keys + `circuit_breaker_history` rows) before and after every test in this file, same technique as `test_concurrent_resilience.py`'s `anthropic_breaker` fixture.

### 1. Failover reliability + switch latency

```python
@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_failover_reliability_and_switch_latency(benchmark_team, anthropic_breaker):
    """Fires N concurrent requests with model="claude-sonnet--fault-error"
    (simulated full outage of the primary for this model) against the real
    gateway. Records, per request: status code and elapsed wall-clock time.
    reliability_pct = 100 * count(status==200) / N -- asserts >=
    thresholds.FAILOVER_RELIABILITY_MIN_PCT (every request should be caught
    by the fallback chain to gpt-4o-mini). failover_switch_p95_seconds =
    the p95 of successful requests' elapsed time (dominated by the 3 primary
    retry attempts' exponential backoff plus one fallback call) -- asserts
    < thresholds.FAILOVER_SWITCH_MAX_SECONDS. write_result() before either
    assertion."""
```

### 2. Recovery latency (real open → half_open → closed)

```python
@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_circuit_breaker_recovery_latency(benchmark_team, anthropic_breaker, db_pool):
    """Drives enough concurrent claude-sonnet--fault-error requests to open
    anthropic's breaker (comfortably exceeding config.circuit_breaker.
    failure_threshold within window_seconds -- read the real values from
    the gateway's own config.yaml via gateway.config.loader rather than
    hardcoding them here). Reads the closed->open row's created_at from
    circuit_breaker_history as t_open. Sleeps past cooldown_seconds (real
    wall-clock wait, same accepted cost as test_concurrent_resilience.py's
    circuit-breaker test). Sends exactly ONE request using the plain,
    healthy "claude-sonnet" model (same anthropic provider, different model
    -- this becomes the half-open probe and succeeds, since claude-sonnet
    has no fault injection). Reads the half_open->closed row's created_at
    as t_closed. recovery_seconds = t_closed - t_open. write_result(), then
    asserts recovery_seconds < config.circuit_breaker.cooldown_seconds +
    thresholds.RECOVERY_MAX_SECONDS_OVER_COOLDOWN (recovery can't be faster
    than the configured cooldown, so this only bounds the overhead added on
    top of it, not a lower bound)."""
```

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build
uv run python3 scripts/setup_demo_teams.py
uv run pytest benchmarks/bench_failover.py -m benchmark -v
docker compose -f deploy/docker-compose.yml down
```

This test file genuinely waits out a real `cooldown_seconds` (30s) — same accepted cost as the existing resilience integration test.

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does the breaker-reset fixture clean up `anthropic`'s Redis keys and `circuit_breaker_history` rows both before and after this file's tests, leaving no residue for other test files or benchmarks that touch the same provider?
   - Does the recovery test read `cooldown_seconds`/`failure_threshold`/`window_seconds` from the loaded config rather than hardcoding values that could drift out of sync with `config.yaml`?
   - Does the recovery test use a genuinely different, healthy model (`claude-sonnet`) as the probe, rather than somehow making `claude-sonnet--fault-error` succeed (which would require touching `mocks/`, out of scope and against ADR-025)?
3. Based on the result, update `phases/benchmarks/index.json` step 3:
   - Success → `"status": "completed"`, `"summary": "one-line summary — file created, measured reliability %, failover-switch p95, and recovery latency, and whether each passed its threshold"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't add a new fault-injection type or touch `mocks/`. Reason: a 500 (`--fault-error`) and a 429 already classify identically as `RetryableProviderError` (`gateway/providers/errors.py`), so a second fault-type model would exercise the exact same gateway code path for no new coverage — not worth the config/scripts changes it would require.
- Don't leave `anthropic`'s circuit breaker open, or leave orphaned `circuit_breaker_history` rows, after this file's tests run. Reason: same shared-state hazard `test_concurrent_resilience.py`'s module docstring already documents — other test files and benchmarks touching the real `anthropic` provider would become order-dependent.
- Don't hardcode `cooldown_seconds`/`failure_threshold`/`window_seconds` as literals in this file. Reason: these values live in `config.yaml` and could change; reading them from the loaded config keeps this benchmark correct if they ever do.
- Do not break existing tests.
