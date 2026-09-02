# Step 2: alerting

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 4's alerting line ("provider error rate above threshold, team approaching budget cap, latency P99 above SLA, circuit breaker opening. Routed to Slack via webhook (env-configured; logs to console/file when not configured) with actionable context")
- `/docs/TRD.md` — §5 (config table — you're adding a new `alerting` block here), §4.1/§4.2 (existing table/Redis-key conventions to match for the new history table and rolling window)
- `/docs/ADR.md` — ADR-014 (Slack webhook, env-gated with console fallback — the alert *sink* is already decided; this step implements it and decides how the two window-based triggers get *evaluated*, which is not yet decided anywhere — add a new ADR entry for that, numbered after step 1's new entry), ADR-025 (mock fault injection is per-request, not visible to out-of-band pings — the same reasoning that kept `health_check.py` independent of `circuit_breaker.py` applies here: this step's error-rate window must be fed from real request outcomes, not from `gateway/resilience/health_check.py`'s ping-based window)
- CLAUDE.md (project root) — "New resilience/rate-limit logic must be covered by the integration test suite" — this step's alert-evaluation logic falls under that
- `gateway/resilience/health_check.py` — read this fully as the *shape* to mirror for the new periodic loop (`_recent_key`-style Redis list, `WINDOW_SIZE`, `_watch_loop`-style infinite loop, `start_*_loop`/`run_*_loop` pair, per-provider try/except so one provider's failure doesn't kill the loop or block others) — and to understand exactly *why* it must NOT be reused directly (its window is fed by out-of-band pings, not real traffic)
- `gateway/resilience/circuit_breaker.py` — `_record_transition`, the one function every breaker state change flows through
- `gateway/ratelimit/budget.py` — `check_budget`, `BudgetStatus` (`.warning` is already computed as "crossed 80%, not yet blocked") — this is called on *every* request, so naively alerting whenever `.warning` is true would spam Slack on every request while a team sits above 80%; you need to alert once per crossing, not once per request
- `gateway/routes.py` — `create_chat_completion`'s budget-check call site (`if budget_status.blocked: ...`) and its response-handling paths, where you'll record real per-request outcomes for the new rolling window
- `gateway/config/loader.py` — `GatewayConfig`, `CircuitBreakerConfig`/`HealthCheckConfig` as the pattern for the new `AlertingConfig` Pydantic model
- `config.yaml`, `tests/fixtures/test_config.yaml` — where the new `alerting:` block goes in both files
- `deploy/schema.sql` — `circuit_breaker_history`/`provider_health_history` table definitions, the pattern to follow for a new `alert_history` table
- `gateway/main.py` — where you'll start the new background loop, alongside `start_health_check_loop`
- `phases/observability/step1.md`'s actual output: `gateway/observability/metrics.py` — the metric names this step's alerts should reference in their Slack message context (e.g. quoting the error rate that triggered an alert)

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

**Design decision already made: the alert sink is one small function, called from four different trigger points.** Two triggers are event-driven (fire exactly where the underlying state change already happens in existing code): circuit-breaker-opens-CLOSED→OPEN, and budget-crosses-80%. Two are window-based aggregates with no single triggering event (provider error rate, latency P99) and need a new periodic evaluator — **Redis-backed, owned by this gateway process, not Grafana Alerting rules against Prometheus.** Reason: ADR-014 already commits to "implement a real Slack-webhook alert sink" as gateway code, and CLAUDE.md requires resilience-adjacent logic to be integration-tested by pytest — alert rules living only in Grafana's provisioning YAML would be untestable by this repo's test suite. Record this as a new ADR entry.

**Design decision already made: the window-based evaluator's data must come from real request outcomes, not `health_check.py`'s ping-based window.** Per ADR-025, `health_check.py` pings are out-of-band and don't see the mocks' per-request fault injection — reusing that window would make "provider error rate above threshold" alerts blind to exactly the outages this project's demo is built to show. Build a separate Redis list, fed from `gateway/routes.py` recording each real attempt's outcome (success/failure + latency), independent of `health_check.py`'s `health:{provider}:recent` key.

**Design decision already made: alert on transition, not on every tick/request while a condition holds.** Firing a Slack message every request while a team is above 80% budget, or every 30s while a provider's error rate stays high, is noise that defeats the point of alerting. Each of the four triggers needs its own "have I already alerted for this specific crossing" state, cleared when the condition resolves, so it can fire again on the *next* crossing.

### 1. Alert sink — `gateway/observability/alerts.py`

```python
async def send_alert(alert_type: str, message: str, context: dict) -> None:
    """POSTs {"text": ...} (or a richer Slack payload, your call) to
    SLACK_WEBHOOK_URL via httpx if the env var is set; otherwise logs the
    alert at WARNING level (console/file fallback, ADR-014). Never raises --
    a broken webhook must not break request handling or the evaluator loop.
    Also writes one best-effort alert_history row (mirror
    circuit_breaker.py's/health_check.py's try/except-log-never-raise
    pattern for the Postgres write)."""
```

`message` should include enough actionable context per the PRD line above (what happened, affected team/provider, current fallback/breaker status where relevant) — put the structured detail in `context` (dict, becomes the `alert_history.context` jsonb column) and a short human-readable summary in `message`.

### 2. Event-driven triggers

- **Circuit breaker opens** — in `gateway/resilience/circuit_breaker.py`'s `_record_transition`, when `to_state == BreakerState.OPEN`, call `send_alert("circuit_breaker_open", ...)` with `provider` and `reason` in context. `_record_transition` already runs exactly once per real transition (not once per request), so no extra dedup is needed here — the state machine itself already only calls this on an actual open.
- **Budget approaching** — in `gateway/ratelimit/budget.py` or `gateway/routes.py` (your call which file owns it — `check_budget` already computes `.warning`, but doesn't have team-scoped "have I alerted this period" state, so it may be cleaner as a thin wrapper called from `routes.py` right after `check_budget`). Dedup with a Redis key scoped to the same period the budget itself uses (e.g. `alerts:budget:{team_id}:{daily,monthly}:{period}`, mirroring `budget.py`'s `_daily_key`/`_monthly_key` period-string convention) so it fires once per day/month per team, not once per request. Only alert on the daily/monthly period whose utilization actually crossed 80% (a team might be fine daily but over monthly, or vice versa).

### 3. Window-based evaluator — `gateway/observability/alert_evaluator.py`

```python
async def record_request_outcome(redis: Redis, provider: str, success: bool, latency_ms: float) -> None:
    """Pushes one outcome into a rolling Redis list (e.g.
    alert_window:{provider}:recent, LPUSH + LTRIM to a bounded size --
    larger than health_check.py's WINDOW_SIZE=5, since this is meant to
    reflect real traffic volume, not a fixed-cadence ping; pick a size and
    justify it in a comment, e.g. last 50 requests). Called from
    gateway/routes.py after every provider_call attempt, success or
    failure -- reuse the same call site/timing as step 1's
    gateway_latency_seconds observation where practical."""

async def evaluate_provider(redis: Redis, provider: str, config: AlertingConfig) -> None:
    """One tick for one provider: reads the rolling window, computes error
    rate and P99 latency (a real percentile over the window, unlike
    health_check.py's single-latest-sample approximation -- the window here
    is large enough for a percentile to be meaningful), compares against
    config.alerting.error_rate_threshold / config.alerting.latency_p99_ms_threshold,
    and calls send_alert on a false->true transition of either condition
    (state tracked in Redis, e.g. alerts:{provider}:error_rate_breached /
    alerts:{provider}:latency_breached, cleared when the condition resolves
    so a later re-breach can alert again)."""

async def run_alert_evaluator_loop(config: GatewayConfig) -> None:
    """Mirrors health_check.py's run_health_check_loop shape: infinite loop,
    sleeps config.alerting.evaluator_interval_seconds between ticks, calls
    evaluate_provider for every configured provider, catches and logs (never
    propagates) any single provider's exception."""

def start_alert_evaluator_loop(config: GatewayConfig) -> asyncio.Task:
    """asyncio.create_task(run_alert_evaluator_loop(config))."""
```

Wire `record_request_outcome` calls into `gateway/routes.py` at the same point step 1's `gateway_latency_seconds` histogram is observed (success and failure paths both count — an evaluator that only sees successes can't compute a real error rate). Wire `start_alert_evaluator_loop(get_config())` into `gateway/main.py`'s `lifespan`, alongside `start_health_check_loop`.

### 4. Config — `gateway/config/loader.py`, `config.yaml`, `tests/fixtures/test_config.yaml`

```python
class AlertingConfig(BaseModel):
    error_rate_threshold: float       # e.g. 0.1 for 10%
    latency_p99_ms_threshold: int     # e.g. 5000
    evaluator_interval_seconds: int   # e.g. 30
```

Add `alerting: AlertingConfig` to `GatewayConfig`, and an `alerting:` block to both `config.yaml` and `tests/fixtures/test_config.yaml` with reasonable demo-friendly values (loose enough that normal test traffic doesn't spuriously trip them, tight enough that a forced-fault test can trip them within a small window).

### 5. Schema — `deploy/schema.sql`

Add an `alert_history` table (`id bigserial PRIMARY KEY`, `alert_type text NOT NULL`, `provider text`, `team_id text REFERENCES teams(id)` — nullable, since circuit-breaker/error-rate alerts aren't team-scoped, `message text NOT NULL`, `context jsonb`, `created_at timestamptz NOT NULL DEFAULT now()`), matching the existing tables' style exactly.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d redis postgres mock-openai mock-anthropic
uv run pytest tests/test_alerts.py tests/test_alert_evaluator.py -v
docker compose -f deploy/docker-compose.yml down
```

Tests (use `httpx`'s mock transport or monkeypatch `httpx.AsyncClient.post` to avoid a real Slack call, per ADR-006's no-real-network rule):
- `SLACK_WEBHOOK_URL` unset → `send_alert` logs instead of POSTing, and still writes an `alert_history` row.
- `SLACK_WEBHOOK_URL` set → `send_alert` POSTs, and a POST failure (mock a raised `httpx` error) doesn't raise out of `send_alert`.
- Forcing a circuit breaker open (reuse `test_circuit_breaker.py`'s `_open_breaker` helper) triggers exactly one `send_alert("circuit_breaker_open", ...)` call.
- A team crossing 80% daily budget triggers exactly one budget alert; a second request in the same day while still above 80% does not trigger a second one; a *new* day (mock/advance the period key) can trigger again.
- `evaluate_provider` on a window with error rate above threshold alerts once, then does not re-alert on the next tick if the rate stays above threshold, but does alert again after a tick where it dropped back below threshold and then breached again.
- `evaluate_provider`'s window is fed only by `record_request_outcome` (real traffic), not by anything in `health_check.py` — a static import check (same style as the resilience phase's `health_check.py`↔`circuit_breaker.py` decoupling test) confirming `gateway/observability/alert_evaluator.py` has no import of `gateway/resilience/health_check.py`.

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does every alert trigger fire once per crossing, never once per request/tick while a condition merely holds?
   - Is the window-based evaluator's data source genuinely independent of `health_check.py`'s ping-based window?
   - Does a Slack webhook failure ever propagate into request handling or crash the evaluator loop?
   - Is the new ADR entry present with the correct next sequential number?
3. Based on the result, update `phases/observability/index.json` step 2:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created/modified, the four trigger points and their dedup mechanism, the alert-evaluator's Redis window design (+ ADR number)"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed (e.g. a real Slack workspace needed for live verification) → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't feed the alert evaluator from `health_check.py`'s Redis window, and don't have `health_check.py` call `send_alert` either. Reason: explicitly decided above (ADR-025) — out-of-band pings don't see per-request fault injection, so they'd make error-rate alerting blind to exactly the failures this project demos.
- Don't alert on every request/tick while a threshold condition merely continues to hold. Reason: explicitly decided above — this would make Slack (or the console fallback) unusable noise within seconds of a real outage.
- Don't implement the window-based alerts as Grafana Alerting rules. Reason: explicitly decided above — untestable by this repo's pytest suite, contradicts ADR-014's "implement a real...alert sink" as gateway-owned code.
- Do not break existing tests.
