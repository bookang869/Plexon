# Plexon

A production-style API gateway that sits in front of an organization's LLM calls. It authenticates teams, enforces per-team rate limits and budgets, transparently routes around provider outages, and gives unified observability across every LLM interaction — regardless of which underlying provider (OpenAI, Anthropic, Ollama) actually served the request.

Callers talk to Plexon exactly like they'd talk to OpenAI's Chat Completions API. Plexon decides which provider actually serves the request, retries and falls back on failure, and meters every call for rate limits, budget, and observability — without the caller ever knowing which provider was behind it.

> Portfolio/demo project, not a production system for a real organization — see [Non-Goals](#non-goals) below.

## Status

Phases 0 (`proxy-layer`), 1 (`ratelimit-budget`), 2 (`resilience`), and 3 (`observability`) are merged; phase 4 (`test-load`) is complete and in this PR — a demo-teams seed script, an integration test suite proving rate-limiting/budget/fallback/circuit-breaker behavior under real concurrent load, a GitHub Actions CI workflow running that suite against real Redis/Postgres/mock-provider containers on every push, and a Locust load test exercising the full stack. This is the last automated Harness phase — see [Build Plan](#build-plan) below.

## Why This Project

Every company with more than one team using LLMs ends up building something like this. It's infrastructure engineering applied to AI: rate limiting, multi-provider failover, and observability — not model-building.

## Core Features

- **Unified proxy layer** — normalizes every request to an OpenAI-compatible schema and translates to/from each provider's native format, so callers never know which provider served them. Supports both streaming and non-streaming responses, and per-team request enrichment (system prompts, compliance disclaimers, rule-based content filters).
- **Rate limiting & budget enforcement** — per-team token-bucket rate limits (requests/min, tokens/min) enforced atomically in Redis, with tiered priority ceilings for `realtime` vs `batch` traffic. Per-team dollar budgets with an 80% warning and a hard block at 100%. Live admin API for adjusting limits without a restart.
- **Fallback & resilience** — background health checks per provider-model combination, fallback chains defined per model tier, exponential-backoff retry (up to 3 attempts) for retryable errors only, and a circuit breaker (closed → open → half-open) per provider with every transition logged.
- **Observability** — OpenTelemetry traces (→ Grafana Tempo) across every request stage, Prometheus metrics (RPS, error rate, latency percentiles, token throughput, cost, fallback rate, circuit-breaker transitions), three provisioned Grafana dashboards (Operations, Business, Performance), and Slack alerting with actionable context.
- **Testing & load** — integration tests against mocked providers with stateless per-request fault injection (no real API keys or network calls required), plus a Locust load test targeting 5,000+ concurrent requests at <10ms gateway overhead.

## Architecture

This diagram reflects what's actually built as of the current phase (see [Build Plan](#build-plan)) — it grows one phase at a time rather than showing the end-state up front.

```mermaid
flowchart TB
    TC[Team Client]
    OP[Operator]

    subgraph GW["Gateway — FastAPI"]
        direction LR
        AUTH["Auth\nteam API key"]
        RATE["Rate Limit\ntiered token bucket"]
        BUDGET["Budget Check\ndaily / monthly spend"]
        ENR["Enrichment\nprompts + content filter"]
        SEL["Provider Select\nmodel → provider"]
        RESIL["Retry + Fallback\nbreaker check → primary (3x)\n→ fallback chain (1x each)"]
        RESP["Response\nnon-stream / SSE"]
        LEDGER["Reconcile + Spend\nrefund + ledger write\n(against serving provider)"]
        AUTH --> RATE --> BUDGET --> ENR --> SEL --> RESIL --> RESP --> LEDGER
    end

    subgraph ADM["Admin API"]
        direction LR
        AAUTH["Admin Auth\nadmin token"]
        AROUTES["limits · spend ·\nteams · audit-log"]
        AAUTH --> AROUTES
    end

    HC["Health Check Loop\n30s ping/provider — dashboard only,\nnever gates routing"]

    subgraph OBS["Observability"]
        direction LR
        TRACE["OTel Spans\nrequest.receipt → auth →\nrate_limit_check → ... → response_delivery"]
        METRICS["/metrics\nrequests · errors · latency ·\ntokens · cost · fallbacks · breaker state"]
        ALERT["Alert Evaluator Loop\n30s tick — error-rate/P99 breach\n→ Slack (+ breaker-open, budget-80%)"]
    end

    subgraph VERIFY["Test & Load ★"]
        direction LR
        CI["CI ★\nGitHub Actions — lint + full suite\nvs. real Redis/Postgres/mocks, every push"]
        ITEST["Integration Suite ★\nconcurrent rate-limit/budget/\nfallback/breaker under real load"]
        LOCUST["Locust ★\n5,000+ concurrent requests,\nmixed teams/models/priorities"]
    end

    REDIS[("Redis\ntoken buckets + spend counters +\ncircuit-breaker state + health status +\nalert windows")]
    PG[("PostgreSQL\nteam config, spend ledger, audit log,\nbreaker/health history, alert history")]
    YAML["YAML Config\nprovider/model map + fallback chains +\npricing + alert thresholds"]
    PROV["Providers\nOpenAI (mock) · Anthropic (mock) · Ollama (real)"]
    TEMPO[("Grafana Tempo\ntrace storage")]
    PROMSVC[("Prometheus\nmetrics scrape")]
    GRAFANA["Grafana Dashboards\nOperations · Business · Performance"]

    TC --> AUTH
    LEDGER --> TC
    OP --> AAUTH
    AUTH <--> PG
    RATE <--> REDIS
    BUDGET <--> REDIS
    RESIL <--> REDIS
    RESIL <--> PG
    RESIL <--> YAML
    RESIL --> PROV
    LEDGER <--> REDIS
    LEDGER <--> PG
    AROUTES <--> PG
    SEL <--> YAML
    HC --> PROV
    HC <--> REDIS
    HC <--> PG
    GW --> TRACE --> TEMPO
    GW --> METRICS --> PROMSVC
    ALERT <--> REDIS
    ALERT --> OP
    PROMSVC --> GRAFANA
    TEMPO --> GRAFANA
    CI --> GW
    ITEST --> GW
    LOCUST --> GW

    classDef new fill:#0f948814,stroke:#0f9488,stroke-width:1.5px,color:inherit;
    class VERIFY,CI,ITEST,LOCUST new;
```
★ = added this phase (`test-load`)

`auth` reads Postgres for team keys/config; `rate limit` and `budget check` run against Redis before the request is allowed to proceed (429 / 402 respectively), gating `enrichment` and `provider select` exactly like before. `provider select` resolves the requested model to a primary provider; `retry + fallback` checks that provider's circuit breaker, retries the primary up to 3x with backoff on transient errors, and on exhaustion walks the model's fallback chain, skipping any provider whose breaker is open. `reconcile + spend` records cost against whichever provider actually served the response; every request opens a root `request.receipt` span exported to Tempo and increments Prometheus counters/histograms/gauges scraped off `/metrics`, feeding three provisioned Grafana dashboards; a background alert evaluator loop pages Slack on an error-rate or P99-latency breach, and the circuit breaker/budget-check paths page Slack directly on breaker-open and 80%-budget-crossed. New this phase: a GitHub Actions CI workflow runs the full lint + test suite against real Redis/Postgres/mock-provider containers on every push; an integration test suite proves rate-limiting, budget enforcement, fallback activation, and circuit-breaker open/close hold up under real concurrent load (not just single-request unit tests); and a Locust scenario drives 5,000+ concurrent requests across mixed teams/models/priorities against the full Docker Compose stack, including a continuous simulated-outage task that keeps exercising the fallback/breaker path throughout the run. Full end-state request-flow spec in [`docs/TRD.md`](docs/TRD.md) §3.

**State is split three ways, by change frequency and durability:**

| Store | Holds | Why |
|---|---|---|
| **Redis** (hot-path) | rate-limit token buckets, circuit-breaker state, provider health status, running spend counter, per-provider alert-evaluation windows | fast-path enforcement only, never the system of record — rebuildable from Postgres |
| **PostgreSQL** (durable) | team configs, per-request spend ledger, admin audit log, circuit-breaker/health history, alert history | source of truth; per-team settings edited live via the admin API, no restart |
| **YAML** (static, hot-reloaded) | provider endpoints, fallback chains, circuit-breaker thresholds, health-check intervals, per-model pricing, alert thresholds | global/rarely-changed settings only, never per-team |

The gateway process itself is stateless — nothing important lives only in memory — so the design supports horizontal scaling without rework, even though the demo runs a single instance.

Full detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (condensed) and [`docs/TRD.md`](docs/TRD.md) (data model, API spec, provider-adapter interface, deployment services).

### Directory Structure

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
└── docs/
```

## Tech Stack

| Layer | Choice |
|---|---|
| Language / framework | Python 3.11+, FastAPI |
| Hot-path store | Redis |
| Durable store | PostgreSQL |
| Static config | YAML, file-watched hot reload |
| Tracing | OpenTelemetry → Grafana Tempo |
| Metrics | Prometheus |
| Dashboards | Grafana |
| Alerting | Slack incoming webhook (env-gated) |
| Providers | OpenAI (mocked), Anthropic (mocked), Ollama (real) |
| Load testing | Locust |
| Retry / backoff | `tenacity` |
| CI | GitHub Actions, real Redis/Postgres service containers |
| Packaging | `uv` |
| Containerization | Docker + Docker Compose |

## API Surface

There is no custom UI. The product surfaces are the gateway API, the admin API, and Grafana's provisioned dashboards.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/v1/chat/completions` | team API key | OpenAI-compatible; `stream: true/false`; optional `X-Priority: realtime\|batch` header |
| GET | `/v1/models` | team API key | models allowed for the caller's team |
| GET | `/healthz` | none | liveness probe |
| GET | `/admin/teams/{team_id}/status` | admin token | current rate-limit/budget status |
| PATCH | `/admin/teams/{team_id}/limits` | admin token | adjust rate limits/budget live |
| GET | `/admin/teams/{team_id}/spend` | admin token | spend dashboard data |
| POST | `/admin/teams` | admin token | create a team |
| GET | `/admin/audit-log` | admin token | query audit history |
| POST | `/admin/config/reload` | admin token | manual YAML reload trigger |

Team API keys and admin tokens are two independent opaque-token systems — never a single token encoding both.

## Getting Started

```bash
uvicorn gateway.main:app --reload               # dev server
pytest                                           # tests (mocked providers — no API keys or network calls needed)
ruff check .                                     # lint
docker compose -f deploy/docker-compose.yml up   # full stack: gateway, Redis, Postgres, Prometheus, Grafana, Tempo, mocks
python3 scripts/setup_demo_teams.py              # seed demo teams with varied rate limits/priorities
```

## Build Plan

Implementation is split into 5 [Harness](.claude/commands/harness.md) phases covering the original build plan, plus a `benchmarks` phase added afterward, each on its own `feat-{phase}` branch, run via `scripts/execute.py`:

| # | Phase | Covers | Status |
|---|---|---|---|
| 0 | `proxy-layer` | Project setup, provider abstraction, auth/routing, streaming passthrough, enrichment | ✅ merged |
| 1 | `ratelimit-budget` | Token buckets, budget caps, tiered limits, admin API | ✅ merged |
| 2 | `resilience` | Health checks, fallback routing, retry/backoff, circuit breakers | ✅ merged |
| 3 | `observability` | OTel spans, Prometheus metrics, Grafana dashboards, alerting | ✅ merged |
| 4 | `test-load` | Integration test suite, Locust load test, full Docker Compose stack | 🔨 this PR |
| 5 | `benchmarks` | Performance benchmark suite: throughput, gateway overhead, failover reliability/recovery, rate-limit/budget accuracy under concurrency | 🔨 this PR |

A final polish phase (demo recording + narrative) is done manually, outside Harness.

## Performance Benchmarks

A manually-invoked suite (`benchmarks/`) that measures gateway performance against a live Docker Compose stack, giving a fast local regression signal separate from CI's correctness tests.

**Prerequisites** — same as `tests/load/locustfile.py`'s: the full stack running and demo teams seeded:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
python3 scripts/setup_demo_teams.py
```

**What's measured:**
- Sustained throughput (requests/sec) against `THROUGHPUT_MIN_RPS`
- Gateway-only overhead latency (P50/P95/P99), read off the `gateway_overhead_seconds` Prometheus histogram
- Failover reliability and switch latency when a provider starts failing
- Circuit-breaker recovery latency after cooldown
- Rate-limit admission accuracy and budget-overshoot bounds under concurrent load

**How to run:**

```bash
uv run pytest benchmarks/ -m benchmark -v
# or
uv run python3 benchmarks/run_all.py
```

Results land in `benchmarks/results/*.json` and `*.csv` (gitignored, regenerated on each run).

This suite is a fast, single-process, locally-run regression signal — it doesn't replace `tests/load/locustfile.py`, which remains the tool covering TRD §10's large-scale "5,000+ concurrent requests" requirement. Per [Non-Goals](#non-goals), raw throughput isn't this project's headline story; these benchmarks exist to catch regressions, not to make a performance claim.

## Non-Goals

- Not a production system for a real organization — no key rotation, no multi-region HA, no real user-account/RBAC system.
- Not a content-moderation project — content filtering is rule-based (keyword/regex), not an ML classifier.
- Not optimizing for raw throughput as the headline story.

## Documentation

| Doc | Contents |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | Product requirements: goals, users, features, success metrics, assumptions, open risks |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Condensed architecture: directory structure, patterns, data flow, state management |
| [`docs/TRD.md`](docs/TRD.md) | Technical reference: system components, request flow, data model (SQL/Redis schemas), API spec, provider-adapter interface, observability detail, deployment |
| [`docs/ADR.md`](docs/ADR.md) | Architecture decision records — the rationale behind every major technical choice |
