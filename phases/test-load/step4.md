# Step 4: locust-load-test

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 5 ("Load test: 5,000+ concurrent requests (Locust) across mixed team keys/models/priorities. Target: <10ms gateway overhead latency. Verify rate-limit accuracy, fallback under simulated outage, and dashboard accuracy under load."), Assumptions ("Load testing tool: Locust")
- `/docs/TRD.md` — §10 (Non-Functional Requirements table: gateway overhead <10ms, 5,000+ concurrent requests), §11 (`tests/load/` — "Locust scenarios" is the intended location)
- `/docs/ADR.md` — ADR-025 (mock fault injection via header **or magic model-name suffix** — "This lets concurrent requests (tests, the Locust load test, the live demo) each get independently-controlled behavior with no race conditions," i.e. this load test is one of the two named consumers of the magic-suffix mechanism), ADR-017 (Ollama isn't installed at plan time — no `ollama` Docker Compose service exists yet; confirm this yourself: `grep -n ollama deploy/docker-compose.yml` returns nothing)
- `phases/test-load/step0.md`'s actual output — `scripts/setup_demo_teams.py`'s `DEMO_TEAMS` roster and `scripts/demo_teams.json`'s exact shape (`[{team_id, name, api_key, rpm_limit, tpm_limit, daily_budget_usd, monthly_budget_usd, allowed_models}, ...]`) — this is what your Locust file's team pool reads.
- `phases/test-load/step2.md`'s actual output — the exact `tests/fixtures/test_config.yaml` diff it made (magic-suffixed model appended to `providers.anthropic.models` and `fallback_chains.fast_tier`). You're making the **same shaped** change to the real `config.yaml` (project root) — same suffix, same append-only pattern, different file.
- `mocks/fault_injection.py` — `FAULT_TYPES`, `_MAGIC_SUFFIX_PREFIX = "--fault-"` — reconfirm `error` (instant 500) is the right choice over `timeout` (30s sleep) for load-generation purposes too.
- `gateway/config/loader.py` — `FallbackChainsConfig`'s two fixed fields (`fast_tier`/`frontier_tier`) — same constraint as step 2, append-only.
- `config.yaml` (project root) — current `providers`/`fallback_chains` blocks you're editing; note `providers.openai.base_url`/`providers.anthropic.base_url` use Compose-internal DNS (`http://mock-openai:8080` etc.) since this file is what the `gateway` container itself loads.
- `deploy/docker-compose.yml` — full current service list (`postgres, redis, mock-openai, mock-anthropic, tempo, gateway, prometheus, grafana` — no `ollama`, no `setup` before step 0 added it). Locust targets `http://localhost:8000` (the `gateway` service's published port), not any internal service name — it runs on the host, outside the Compose network.
- `gateway/ratelimit/limiter.py` — `resolve_tier`/`X-Priority` header (`realtime`/`batch`) — this is how the load test drives "mixed... priorities."
- `pyproject.toml` — current `[dependency-groups].dev` — you're adding `locust`.

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

### 1. Add `locust` to `pyproject.toml`

Add `"locust"` to `[dependency-groups].dev`, then `uv sync` so `uv.lock` picks it up.

### 2. Edit `config.yaml` (project root) — mirror step 2's fixture change

Same two additive edits step 2 made to `tests/fixtures/test_config.yaml`, applied here:
- Append `claude-sonnet--fault-error` to `providers.anthropic.models`.
- Append `anthropic:claude-sonnet--fault-error, openai:gpt-4o-mini, ollama:llama3` to the end of `fallback_chains.fast_tier`'s existing list.

Don't touch `frontier_tier` or any other block.

### 3. Extend `scripts/setup_demo_teams.py`'s demo-realtime-highvolume team

That team's `allowed_models` already "spans all five configured models" per step 0's spec — append `"claude-sonnet--fault-error"` to it, so this one team can be used for both normal mixed traffic and the simulated-outage task below (no need for a fifth team). After this edit, re-run `uv run python3 scripts/setup_demo_teams.py` so `scripts/demo_teams.json` picks up the updated `allowed_models` before Locust runs against it.

### 4. Create `tests/load/locustfile.py`

```python
"""Locust load-test scenario (PRD Core Feature 5, TRD SS10): 5,000+ concurrent
requests across mixed team keys/models/priorities against a live gateway
(`docker compose -f deploy/docker-compose.yml up -d`, host http://localhost:8000).

Team pool: read from scripts/demo_teams.json (step 0's setup script writes this
on the host; run `uv run python3 scripts/setup_demo_teams.py` before this file
-- Locust runs on the host, outside the Compose network, and needs the
host-visible copy of that file, not the one the `setup` Compose service writes
inside its own container).

`ollama` has no live Docker Compose service yet (ADR-017) -- this scenario
deliberately never requests `llama3` directly as a primary model (it would
just generate transport-error noise unrelated to what's being load-tested);
it remains configured as the tail of `fast_tier`'s fallback chain but is never
reached in practice (anthropic and openai are both live).

Run: `uv run locust -f tests/load/locustfile.py --host http://localhost:8000
-u 5000 -r 100 --run-time 3m --headless --csv=tests/load/results`
(5,000 concurrent users, ramping 100/s, 3-minute steady state, per TRD SS10's
"5,000+ concurrent requests" target -- for a quick smoke run during
development, use much smaller -u/-r/--run-time values instead).

Gateway-overhead latency (target <10ms, TRD SS10): Locust's own response-time
percentiles measure end-to-end latency (gateway + mocked-provider round
trip), not gateway overhead in isolation -- there's no separate
gateway-only timer exposed today (gateway_latency_seconds, from the
observability phase, measures the same end-to-end call). To estimate
overhead, this file also runs a small-weight task hitting mock-openai
directly, bypassing the gateway entirely -- comparing that task's median
latency against the normal chat-completion task's median in the Locust
report approximates gateway overhead. This is an approximation, not an
exact isolated measurement; note that plainly in the results, don't
present it as a precise <10ms figure.
"""

class GatewayUser(HttpUser):
    """Normal team traffic: chat completions across each team's own
    allowed_models (excluding the magic fault-injection model -- that's
    driven by a separate low-weight task below), mixed X-Priority tiers."""

    @task(<high weight>)
    def chat_completion(self): ...

    @task(<low weight>)
    def chat_completion_batch_priority(self): ...  # X-Priority: batch

    @task(<small weight>)
    def simulated_outage_request(self):
        """Requests claude-sonnet--fault-error against demo-realtime-highvolume
        (the only team whose allowed_models includes it) -- every real
        attempt against anthropic fails and falls back to gpt-4o-mini,
        continuously exercising the fallback path (and, at sustained volume,
        the circuit breaker) throughout the run, simulating a
        provider partially down for the whole test rather than a scripted
        time-boxed outage window."""


class DirectMockUser(HttpUser):
    """Low-weight baseline: hits mock-openai directly (bypassing the gateway)
    to establish a provider-latency floor for the gateway-overhead estimate
    described above."""

    @task
    def direct_mock_call(self): ...
```

Leave the exact weights/task bodies to your discretion (signature-level spec above) — but every request must:
- Use a real team's `api_key` from `scripts/demo_teams.json` (`Authorization: Bearer {api_key}`), picked per-request or per-simulated-user from the team pool (not one fixed team for the whole run — PRD asks for "mixed team keys").
- Only request a `model` that's actually in that specific team's `allowed_models` (per-team lists differ, per step 0) — a request for a disallowed model gets a `403`, which would pollute the load test's error rate with a self-inflicted, uninteresting failure.
- Randomize `X-Priority` between omitted (defaults `realtime`) and `"batch"` across requests, not fixed per user, so both tiers see real concurrent traffic.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build postgres redis mock-openai mock-anthropic gateway
uv run python3 scripts/setup_demo_teams.py
uv run locust -f tests/load/locustfile.py --host http://localhost:8000 -u 20 -r 10 --run-time 15s --headless
docker compose -f deploy/docker-compose.yml down
```

This is a smoke run (20 users, 15s) proving the scenario is wired correctly end-to-end with zero errors reported in Locust's summary table — not the full 5,000-user run described in the module docstring, which is documented as the target invocation but is a manual/demo-time exercise (matches this phase's step 0-3 convention of keeping automated ACs fast; a 5,000-user 3-minute run is appropriate to run once by hand before the phase is considered demo-ready, not on every automated pass).

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does `config.yaml`'s `fallback_chains.fast_tier` retain its original three entries before the four new ones (same append-only rule as step 2)?
   - Does every Locust task pick a model from that specific request's team's own `allowed_models`, not a fixed global model list?
   - Does the docstring honestly describe the gateway-overhead measurement as an approximation, not fabricate a precise isolated-overhead metric that doesn't exist?
3. Based on the result, update `phases/test-load/index.json` step 4:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created/edited, the config.yaml diff, task weights, the gateway-overhead approximation approach -- marking the test-load phase complete"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't request `llama3` as a primary model from any Locust task. Reason: no `ollama` Compose service exists yet (ADR-017) — every such request would fail on a DNS/connection error unrelated to anything this load test is meant to exercise, polluting the error-rate signal with noise.
- Don't fabricate a precise "<10ms gateway overhead" number from a measurement this codebase doesn't actually expose. Reason: `gateway_latency_seconds` (from the observability phase) times the whole call including the mocked-provider round trip, not gateway-only processing — presenting an unverified precise figure would misrepresent what was actually measured; state the direct-vs-gateway comparison as an approximation instead.
- Don't hardcode a single team's credentials for the whole run. Reason: PRD explicitly asks for "mixed team keys" — a single-team run wouldn't exercise per-team rate-limit/budget isolation under load, one of the things TRD SS10 wants verified.
- Do not break existing tests.
