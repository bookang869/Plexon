# PRD: LLM Gateway with Rate Limiting, Fallback Routing, and Observability

## Goal

A production-style API gateway that sits in front of an organization's LLM calls. It authenticates teams, enforces per-team rate limits and budgets, transparently routes around provider outages, and gives unified observability across every LLM interaction — regardless of which underlying provider (OpenAI, Anthropic, Ollama) actually served the request.

- Demonstrate production infrastructure engineering skill applied to an AI systems problem: rate limiting, multi-provider failover, and observability, not model-building.
- Present a believable, demoable system: a 4-minute recording should show real-time metrics, a simulated outage triggering fallback, rate limiting kicking in, and a circuit breaker opening and recovering.
- Produce a codebase and narrative usable directly in technical interviews ("I built an LLM API gateway with automatic multi-provider failover and per-team budget enforcement, <10ms overhead").

## Users

- **Team callers** — services/apps within the org that need to call an LLM. Authenticate with a team API key. Never need to know which provider actually served their request.
- **Admins/operators** — manage team configs (rate limits, budgets, allowed models), view spend dashboards, respond to alerts. Authenticate with a named admin token, separate from any team key.

## Core Features

1. **Unified Proxy Layer**
   - Normalize incoming requests to an OpenAI-compatible schema; translate to/from each provider's native format (Anthropic requires real translation; Ollama is near-native).
   - Authenticate every request via team API key; look up the team's allowed models/providers, rate limits, and budget.
   - Support both streaming and non-streaming responses. Streaming responses are translated to OpenAI-style SSE chunks per-provider in real time and simultaneously logged in full once complete.
   - Support per-team request enrichment: injected system prompts, compliance disclaimers, and rule-based (keyword/regex) content filters, centrally configured. Global defaults live in YAML; per-team overrides live in a `config jsonb` column on the `teams` table (ADR-021).

2. **Rate Limiting & Budget Enforcement**
   - Per-team token bucket rate limiting (requests/min, tokens/min) enforced atomically via Redis; `429` + `Retry-After` on limit breach.
   - Per-team monthly/daily dollar budgets, computed from `input tokens × input price + output tokens × output price`. Warning at 80% utilization; hard block at 100% with a clear error.
   - Tiered priority: each request-priority tier gets its own ceiling within a team's overall rate limit (e.g., low-priority batch traffic capped below what real-time traffic can use), so high-priority requests keep working under pressure. Requests exceeding their tier's ceiling are rejected immediately (429) — not queued. Tier is signaled via an `X-Priority: realtime|batch` request header, defaulting to `realtime` (ADR-020).
   - Admin API: view rate-limit status per team, adjust limits/budgets live (no restart), view spend dashboards, configure approaching-limit alerts. All changes logged with admin identity and timestamp.

3. **Fallback & Resilience**
   - Background health checks every 30s per provider-model combination; status (`healthy`/`degraded`/`down`) and rolling error-rate/P99 latency tracked; history persisted for post-incident analysis.
   - Fallback chains defined per model tier (not per specific model) in global config.
   - Retry primary provider with exponential backoff (up to 3 attempts) before falling back, only for retryable errors (rate limits, timeouts) — not for non-retryable errors (auth failures, content policy violations).
   - Circuit breaker per provider: opens after N failures in M seconds, routes all traffic to fallbacks, half-open test request after cooldown, closes on success. Every state transition is logged and emitted as a Prometheus metric.

4. **Observability**
   - OpenTelemetry spans for every request stage (receipt, auth, rate-limit check, provider selection, LLM call, response processing, delivery), each carrying team ID, model requested/served, token counts, latency, cost.
   - Traces exported to Grafana Tempo for waterfall visualization.
   - Prometheus metrics: RPS by team/model/provider, error rate by team/model/provider/error-type, latency P50/P95/P99 by provider, token throughput, cost per team per day, fallback trigger rate, circuit-breaker state changes.
   - Three Grafana dashboards: Operations (provider health, error rates, fallback events, circuit-breaker status), Business (per-team spend, budget utilization, usage trends), Performance (latency percentiles, token throughput).
   - Alerting: provider error rate above threshold, team approaching budget cap, latency P99 above SLA, circuit breaker opening. Routed to Slack via webhook (env-configured; logs to console/file when not configured) with actionable context (what happened, affected teams, fallback status).

5. **Testing & Load**
   - Integration tests: rate limiting under concurrent load, budget cap enforcement, fallback activation, circuit breaker open/close, streaming integrity — using mocked providers with fault injection. Fault injection is stateless and per-request (`X-Mock-Fault` header or magic model name, ADR-025), so concurrent tests/requests each control their own simulated outcome independently.
   - Load test: 5,000+ concurrent requests (Locust) across mixed team keys/models/priorities. Target: <10ms gateway overhead latency. Verify rate-limit accuracy, fallback under simulated outage, and dashboard accuracy under load.
   - Full stack containerized via Docker Compose: gateway, Redis, Postgres, Prometheus, Grafana, Tempo, mock provider endpoints. Setup script creates demo teams with varied rate limits/priorities.
   - CI (GitHub Actions) runs the integration suite against real Redis/Postgres service containers, not in-memory substitutes (ADR-022).

## Out of Scope for MVP

- Not a production system for a real organization — no key rotation, no multi-region HA, no real user-account/RBAC system.
- Not a content-moderation project — content filtering is rule-based (keyword/regex), not an ML classifier.
- Not optimizing for raw throughput as the primary story (that ground is already covered by an existing Go project in the author's portfolio).

## Design

No custom UI. Product surfaces are the OpenAI-compatible gateway API, the admin API, and Grafana's provisioned dashboards (Operations, Business, Performance) — see `docs/UI_GUIDE.md` for the (currently unused) UI convention placeholder.

## Architecture Overview

- **Language/framework:** Python 3.11+, FastAPI.
- **Hot-path state:** Redis — rate-limit counters, circuit-breaker current state, provider health status, spend running-counter.
- **Durable state:** Postgres — team configs, per-request spend ledger, audit log, circuit-breaker/health history.
- **Static config:** YAML — provider endpoints, fallback chains per tier, circuit-breaker thresholds, health-check intervals, per-model pricing. Hot-reloaded on file change.
- **Providers:** OpenAI-compatible wire format throughout; Anthropic/Claude treated as the preferred/default provider; OpenAI and Anthropic mocked with stateless per-request fault injection for dev/test/demo (fabricated token usage); Ollama integrated live (native token usage), with live-call verification deferred until it's installed locally.
- **Deployment topology:** single gateway instance for the demo; no in-process state, so the design supports horizontal scaling without rework.
- **Auth:** two independent opaque-token systems — team API keys and named admin tokens — each backed by its own lookup table.

See `docs/ARCHITECTURE.md` and `docs/TRD.md` for full technical detail.

## Success Metrics

- Gateway overhead latency <10ms under load.
- Rate limiting and budget enforcement verified accurate under 5,000+ concurrent requests.
- Fallback and circuit-breaker behavior demonstrably correct under simulated outage.
- Dashboards match reality under load.
- <4-minute demo recording showing: live metrics, simulated outage → fallback, rate limiting triggering, circuit breaker opening/recovering.

## Assumptions

- Load testing tool: Locust.
- Retry/backoff implementation: standard exponential backoff (e.g., `tenacity`).
- Health-check loop: asyncio background task within the single gateway process.
- CI: GitHub Actions running the full test suite on push (viable without secrets since providers are mocked), against real Redis/Postgres service containers (ADR-022).
- Python packaging: `uv` (ADR-023).
- Per-model $/token pricing lives in YAML, using real published list prices — mocking providers avoids being billed, it doesn't remove the need for a cost figure to enforce budgets against (ADR-018).
- Ollama isn't installed on the dev machine at plan time: the real adapter is built now, but live-call verification is a manual step done once it's installed (ADR-017).
- Implementation is split into 5 Harness phases (`proxy-layer`, `ratelimit-budget`, `resilience`, `observability`, `test-load`); Phase 6 (demo recording + narrative) is done manually, not as a Harness phase (ADR-024).

## Open Risks

- Streaming translation across three different provider stream formats is the most implementation-heavy piece of Phase 1 — budget extra time here.
- Grafana Tempo is a new addition to the stack beyond the original spec's tech table; adds one more Docker Compose service and slightly more setup-script work.
