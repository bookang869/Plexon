# TRD: LLM Gateway with Rate Limiting, Fallback Routing, and Observability

Technical reference for implementation. Derived from `docs/PRD.md` (requirements) and `docs/ADR.md` (decision rationale). See `docs/ARCHITECTURE.md` for the condensed, step-prompt-sized version of this document. Where this document adds concrete specifics (schemas, endpoint paths, key naming) beyond what was explicitly discussed, those are proposed defaults, not grilled decisions — flag anything you want to change before implementation starts.

## 1. Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | ADR-001 |
| Web framework | FastAPI | async-native |
| Hot-path store | Redis | rate limits, circuit-breaker state, health status, spend counter |
| Durable store | PostgreSQL | team config, spend ledger, audit log, history |
| Static config | YAML (file-watched hot reload) | global/static settings only |
| Tracing | OpenTelemetry SDK → Grafana Tempo | ADR-013 |
| Metrics | Prometheus (`prometheus-client` / OTel metrics exporter) | scraped by Prometheus server |
| Dashboards | Grafana | Operations, Business, Performance |
| Alerting | Slack incoming webhook (env-gated) | ADR-014 |
| Providers | OpenAI (mocked), Anthropic (mocked), Ollama (real) | ADR-006 |
| Load testing | Locust | |
| Retry/backoff | `tenacity` | |
| CI | GitHub Actions (real Redis/Postgres service containers) | ADR-016, ADR-022 |
| Containerization | Docker + Docker Compose | |
| Packaging | `uv` | ADR-023 |

## 2. System Components

```
                         ┌─────────────────┐
 Team clients ─────────▶ │   Gateway (FastAPI)  │
                         │  - auth              │
                         │  - rate limit check   │
                         │  - provider router    │
                         │  - circuit breaker    │
                         │  - enrichment/filter  │
                         │  - stream translator  │
                         └───┬─────────┬────────┘
                             │         │
                 ┌───────────┘         └───────────┐
                 ▼                                  ▼
         ┌───────────────┐                 ┌────────────────────┐
         │     Redis      │                 │     PostgreSQL       │
         │ rate buckets   │                 │ team config           │
         │ breaker state  │                 │ spend ledger           │
         │ health status  │                 │ audit log               │
         │ spend counter  │                 │ breaker/health history  │
         └───────────────┘                 └────────────────────┘

         ┌──────────────────────────────────────────────────┐
         │  Providers: OpenAI (mock) · Anthropic (mock) · Ollama (real) │
         └──────────────────────────────────────────────────┘

         ┌──────────────────────────────────────────────────┐
         │  Observability: OTel → Tempo (traces) · Prometheus (metrics) │
         │                 → Grafana (dashboards) → Slack (alerts)      │
         └──────────────────────────────────────────────────┘
```

Single gateway instance for the demo; no in-process state (ADR-007), so the design supports horizontal scaling without rework.

## 3. Request Flow (non-streaming)

1. **Receipt** — request hits `POST /v1/chat/completions` (OpenAI-compatible schema).
2. **Authentication** — team API key from `Authorization` header, looked up against the team-key table.
3. **Rate-limit check** — priority tier read from the `X-Priority: realtime|batch` request header (defaults to `realtime`, ADR-020); Redis token-bucket check for the team, at that tier's ceiling (ADR-011). `429` + `Retry-After` on breach.
4. **Budget check** — team's spend counter (Redis) checked against Postgres-sourced budget; warn at 80%, block at 100%.
5. **Enrichment** — inject configured system prompt/disclaimers; apply rule-based content filter (ADR-015).
6. **Provider selection** — resolve requested model to a provider via YAML fallback-chain config; consult circuit-breaker state (ADR-010) for the primary provider.
7. **Call + retry** — call primary provider; on retryable error (timeout, rate limit), retry with exponential backoff up to 3 attempts (`tenacity`); on exhausted retries or non-retryable error, fall back per the tier's fallback chain.
8. **Response translation** — normalize provider response back to OpenAI-compatible schema.
9. **Logging** — write spend ledger row (Postgres) and emit OTel span + Prometheus metrics for the full request.
10. **Delivery** — return response to caller.

Streaming follows the same steps 1-6, then per-provider stream chunks are translated to OpenAI-style SSE in real time while being teed into a buffer for step 9 (ADR-009).

## 4. Data Model

### 4.1 PostgreSQL Schema

```sql
-- Team registry & config (admin-editable, no restart required)
teams (
  id              text PRIMARY KEY,        -- e.g. "team-acme"
  name            text NOT NULL,
  allowed_models  text[] NOT NULL,
  rpm_limit       int NOT NULL,
  tpm_limit       int NOT NULL,
  daily_budget_usd  numeric,
  monthly_budget_usd numeric,
  config          jsonb NOT NULL DEFAULT '{}',  -- per-team enrichment/content-filter overrides (ADR-021)
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
)

-- Team API keys (opaque token -> team), separate from admin tokens (ADR-012)
team_api_keys (
  token       text PRIMARY KEY,
  team_id     text NOT NULL REFERENCES teams(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz
)

-- Named admin tokens (opaque token -> admin identity), ADR-012
admin_tokens (
  token       text PRIMARY KEY,
  admin_name  text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz
)

-- Per-request spend ledger, source of truth for budget/spend (ADR-004)
spend_ledger (
  id              bigserial PRIMARY KEY,
  team_id         text NOT NULL REFERENCES teams(id),
  provider        text NOT NULL,
  model           text NOT NULL,
  input_tokens    int NOT NULL,
  output_tokens   int NOT NULL,
  cost_usd        numeric NOT NULL,
  request_id      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
)

-- Admin API audit log (who changed what, when) (ADR-012)
audit_log (
  id          bigserial PRIMARY KEY,
  admin_name  text NOT NULL,
  action      text NOT NULL,          -- e.g. "update_rate_limit"
  team_id     text REFERENCES teams(id),
  before      jsonb,
  after       jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
)

-- Circuit-breaker state-change history (ADR-010)
circuit_breaker_history (
  id          bigserial PRIMARY KEY,
  provider    text NOT NULL,
  from_state  text NOT NULL,          -- closed | open | half_open
  to_state    text NOT NULL,
  reason      text,
  created_at  timestamptz NOT NULL DEFAULT now()
)

-- Provider health history, for post-incident analysis
provider_health_history (
  id            bigserial PRIMARY KEY,
  provider      text NOT NULL,
  model         text,
  status        text NOT NULL,        -- healthy | degraded | down
  error_rate    numeric,
  p99_latency_ms int,
  created_at    timestamptz NOT NULL DEFAULT now()
)
```

### 4.2 Redis Key Schema

| Key pattern | Type | Purpose | TTL |
|---|---|---|---|
| `ratelimit:{team_id}:{tier}:rpm` | token bucket (sorted set / counter) | requests-per-minute enforcement per tier | rolling window |
| `ratelimit:{team_id}:{tier}:tpm` | token bucket | tokens-per-minute enforcement per tier | rolling window |
| `spend:{team_id}:{period}` | counter | fast-path running spend total | until period rollover |
| `breaker:{provider}:state` | string (`closed`\|`open`\|`half_open`) | current circuit-breaker state | none (explicit transitions) |
| `breaker:{provider}:failures` | counter | failure count in current window | window length (M seconds) |
| `health:{provider}:{model}:status` | string | current health status | 30s+ (refreshed by health-check loop) |

## 5. Configuration (YAML)

Global/static config only (ADR-005); per-team settings live in Postgres via the admin API.

```yaml
providers:
  openai:
    base_url: "http://mock-openai:8080"
    models: ["gpt-4o", "gpt-4o-mini"]
  anthropic:
    base_url: "http://mock-anthropic:8080"
    models: ["claude-opus", "claude-sonnet"]
  ollama:
    base_url: "http://ollama:11434"
    models: ["llama3"]

fallback_chains:
  fast_tier: [anthropic:claude-sonnet, openai:gpt-4o-mini, ollama:llama3]
  frontier_tier: [anthropic:claude-opus, openai:gpt-4o]

circuit_breaker:
  failure_threshold: 5      # N failures
  window_seconds: 60        # in M seconds
  cooldown_seconds: 30      # before half-open probe

health_check:
  interval_seconds: 30

priority_tiers:
  realtime:  { rpm_ceiling_pct: 100 }
  batch:     { rpm_ceiling_pct: 60 }

enrichment_defaults:
  content_filter:
    enabled: true
    blocklist: []            # per-team overrides live in Postgres (teams.config, ADR-021)

# Per-model $/token, real published list prices (ADR-018). Applies uniformly
# to mocked and real providers alike -- mocking avoids being billed, it
# doesn't remove the need for a plausible cost figure to enforce budgets against.
pricing:
  openai:
    gpt-4o:       { input_per_1k: 0.0025, output_per_1k: 0.01 }
    gpt-4o-mini:  { input_per_1k: 0.00015, output_per_1k: 0.0006 }
  anthropic:
    claude-opus:   { input_per_1k: 0.015, output_per_1k: 0.075 }
    claude-sonnet: { input_per_1k: 0.003, output_per_1k: 0.015 }
  ollama:
    llama3: { input_per_1k: 0.0, output_per_1k: 0.0 }
```

**Mock fault injection (ADR-025):** `mock-openai`/`mock-anthropic` are stateless with respect to fault behavior -- each request controls its own simulated outcome via a `X-Mock-Fault: timeout|error|rate_limit` header (or a magic model-name suffix), rather than any shared/global toggle. This lets concurrent requests (tests, the Locust load test, the live demo) each get independently-controlled behavior with no race conditions.

## 6. API Specification

### 6.1 Gateway API (team-facing, OpenAI-compatible)

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/v1/chat/completions` | team API key | `stream: true/false` supported; optional `X-Priority: realtime\|batch` header (ADR-020, defaults to `realtime`) |
| GET | `/v1/models` | team API key | models allowed for the caller's team |
| GET | `/healthz` | none | liveness probe |

### 6.2 Admin API (operator-facing)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/admin/teams/{team_id}/status` | admin token | current rate-limit/budget status |
| PATCH | `/admin/teams/{team_id}/limits` | admin token | adjust rate limits/budget live; writes `audit_log` |
| GET | `/admin/teams/{team_id}/spend` | admin token | spend dashboard data |
| POST | `/admin/teams` | admin token | create a team |
| GET | `/admin/audit-log` | admin token | query audit history |
| POST | `/admin/config/reload` | admin token | manual YAML reload trigger (in addition to file-watch) |

## 7. Provider Adapter Interface

```python
class ProviderAdapter(Protocol):
    async def chat_completion(self, request: OpenAIChatRequest) -> OpenAIChatResponse: ...
    async def chat_completion_stream(self, request: OpenAIChatRequest) -> AsyncIterator[OpenAIChatChunk]: ...
    async def health_check(self) -> HealthStatus: ...
```

One implementation per provider (`OpenAIAdapter`, `AnthropicAdapter`, `OllamaAdapter`); each owns translation to/from its native format and back to the OpenAI-compatible schema (ADR-008). `OpenAIAdapter`/`AnthropicAdapter` talk to the fault-injectable mocks and fabricate a plausible `usage` object; `OllamaAdapter` talks to a real local server and translates its native `prompt_eval_count`/`eval_count` into `usage` (ADR-019). Since Ollama isn't installed on the dev machine at plan time, `OllamaAdapter`'s automated acceptance criteria is unit-level (stubbed HTTP response) — live-call verification against a real running Ollama server is a manual step (ADR-017).

## 8. Observability Detail

**OTel spans per request:** `request.receipt` → `auth` → `rate_limit_check` → `provider_selection` → `provider_call` → `response_processing` → `response_delivery`. Attributes on each: `team_id`, `model_requested`, `model_served`, `input_tokens`, `output_tokens`, `latency_ms`, `cost_usd`.

**Prometheus metrics:**
- `gateway_requests_total{team, model, provider}`
- `gateway_errors_total{team, model, provider, error_type}`
- `gateway_latency_seconds{provider}` (histogram, for P50/P95/P99)
- `gateway_tokens_total{team, direction}` (input/output)
- `gateway_cost_usd_total{team}`
- `gateway_fallback_triggered_total{from_provider, to_provider}`
- `gateway_circuit_breaker_state{provider}` (gauge: 0=closed, 1=half_open, 2=open)
- `gateway_circuit_breaker_transitions_total{provider, from_state, to_state}`

**Grafana dashboards:** Operations (provider health, error rates, fallback events, circuit-breaker status), Business (per-team spend, budget utilization, usage trends), Performance (latency percentiles, token throughput).

## 9. Deployment (Docker Compose services)

- `gateway` — the FastAPI app
- `redis`
- `postgres`
- `prometheus`
- `grafana` (preconfigured dashboards provisioned on boot)
- `tempo` (ADR-013)
- `mock-openai`, `mock-anthropic` — fault-injectable mock providers
- `ollama` — real local model server
- `setup` — one-shot script creating demo teams with varied rate limits/priorities

## 10. Non-Functional Requirements

| Requirement | Target |
|---|---|
| Gateway overhead latency | <10ms |
| Concurrent load | 5,000+ concurrent requests |
| Rate-limit accuracy | correct under concurrent load (no over/under-admission) |
| Circuit breaker | opens/closes correctly under injected faults |
| Streaming | passes through without corruption under load |
| CI | test suite runs with no external secrets (mocked providers); Redis/Postgres run as real GitHub Actions service containers (ADR-022) |

## 11. Proposed Project Structure

```
plexon/
├── gateway/
│   ├── main.py
│   ├── auth/            # team key + admin token lookup
│   ├── ratelimit/        # Redis token bucket, tier ceilings
│   ├── providers/        # adapters: openai, anthropic, ollama
│   ├── resilience/       # circuit breaker, retry/backoff, health checks
│   ├── enrichment/       # system prompts, content filter
│   ├── admin/            # admin API routes
│   ├── observability/    # OTel spans, Prometheus metrics
│   └── config/           # YAML loader + hot reload
├── mocks/                 # mock OpenAI/Anthropic servers with fault injection
├── tests/
│   ├── integration/
│   └── load/               # Locust scenarios
├── deploy/
│   ├── docker-compose.yml
│   ├── grafana/            # provisioned dashboards
│   └── prometheus/
├── scripts/
│   └── setup_demo_teams.py
├── PRD.md
├── ADR.md
└── TRD.md
```

## 12. Harness Phase Plan

Per ADR-024, the build is split into 5 Harness phases (`phases/{dir}/`), each its own `feat-{phase}` branch, executed in order via `scripts/execute.py`:

| # | Phase dir | Covers |
|---|---|---|
| 0 | `proxy-layer` | Provider abstraction, auth/routing, streaming passthrough, enrichment (project setup — `pyproject.toml` via `uv`, directory skeleton — is step 0) |
| 1 | `ratelimit-budget` | Token buckets, budget caps, tiered limits (`X-Priority` header), admin API |
| 2 | `resilience` | Health checks, fallback routing, retry/backoff, circuit breakers |
| 3 | `observability` | OTel spans, Prometheus metrics, Grafana dashboards, alerting |
| 4 | `test-load` | Integration test suite, Locust load test, full Docker Compose stack |

PRD Phase 6 (demo recording + narrative) is manual and out of scope for Harness.
