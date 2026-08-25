# Architecture

Condensed overview. For full data model (SQL/Redis schemas), API spec, provider-adapter interface, observability detail, and deployment services, see `docs/TRD.md`.

## Directory Structure
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

## Patterns
- **Provider adapter pattern**: one `ProviderAdapter` implementation per provider (`OpenAIAdapter`, `AnthropicAdapter`, `OllamaAdapter`), each owning translation to/from its native format and the OpenAI-compatible canonical schema (ADR-008). New providers plug in without touching gateway core logic.
- **Stateless gateway process**: no rate-limit counts, circuit-breaker state, or provider health may live only in a single process's memory — everything goes through Redis, so the design supports horizontal scaling without rework even though the demo runs one instance (ADR-007).
- **Two-tier config**: global/static settings (provider endpoints, fallback chains, circuit-breaker thresholds) live in hot-reloaded YAML; per-team/dynamic settings (rate limits, budgets, allowed models) live in Postgres, edited live via the admin API (ADR-005).

## Data Flow
```
team client → auth (team API key) → rate-limit check (Redis, per-tier ceiling)
→ budget check (Redis + Postgres) → enrichment (system prompt / content filter)
→ provider selection (YAML fallback chain + circuit-breaker state)
→ provider call (retry w/ backoff → fallback on exhaustion) → response translation
→ spend ledger write (Postgres) + OTel span / Prometheus metrics → client
```
Streaming follows the same path through provider selection, then translates each provider's native stream chunks to OpenAI-style SSE in real time while teeing into a buffer for the post-stream log/metrics write (ADR-009). See `docs/TRD.md` §3 for the full numbered request-flow spec.

## State Management
- **Redis (hot-path)**: rate-limit token buckets, circuit-breaker current state, provider health current status, spend running-counter — fast-path enforcement only, never the system of record (ADR-004).
- **Postgres (durable)**: team configs (including a `config jsonb` column for per-team enrichment/content-filter overrides, ADR-021), per-request spend ledger (source of truth for budget/spend), admin audit log, circuit-breaker/health history. Redis counters can be rebuilt from Postgres after a restart.
- **YAML (static)**: provider endpoints, fallback chains per tier, circuit-breaker thresholds, health-check intervals, per-model $/token pricing (ADR-018) — hot-reloaded on file change, never per-team.

## Request Metadata
- Priority tier (`realtime`/`batch`) is signaled per-request via an `X-Priority` header, not a body field — keeps the request body strictly OpenAI-compatible (ADR-020).
- Mock providers (`mock-openai`, `mock-anthropic`) are stateless with respect to fault behavior: each request controls its own simulated outcome via a header/magic-value trigger, not shared/global toggle state (ADR-025).
