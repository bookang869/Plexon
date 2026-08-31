# Architecture Decision Records

Decisions for the LLM Gateway project, recorded via a `/grill-me`-style design interview. Each ADR is Accepted unless noted otherwise.

## Philosophy

This is a portfolio/demo system, not production infrastructure for a real organization (ADR-002). Optimize for correctness and production-grade *behavior* in the parts that are the point of the project — rate limiting, fallback, circuit breakers, observability — and skip hardening that isn't relevant to that story (secret rotation, multi-region HA, full RBAC). Keep the full 6-phase scope (ADR-003) but treat the timeline as flexible; defer scope cuts until it's clear what's actually taking time. Prefer reusing existing, well-understood mechanisms (e.g., the Redis token bucket for tiered rate limits, ADR-011) over building new coordination machinery, and prefer options that keep tests/CI free of cost and external secrets (ADR-006).

---

### ADR-001: Language & Framework — Python 3.11+ with FastAPI

**Context:** The project spec allowed either Python 3.11+/FastAPI or Go/`net/http`. The author has an existing portfolio project (a self-healing pipeline) already built in Go specifically to demonstrate speed/production-engineering skill.

**Decision:** Build the gateway in Python 3.11+ using FastAPI.

**Consequences:** Faster access to first-class OpenAI/Anthropic SDKs, tokenizer libraries, and OTel instrumentation, at some cost in raw per-request throughput versus Go. Diversifies the author's portfolio narrative (AI-systems understanding) rather than repeating the "I write fast code" story already told by the Go project.

---

### ADR-002: Target Rigor — Portfolio/Demo System, Not Production

**Context:** The spec frames the project as an interview/portfolio artifact (explicit "Polish for Portfolio" phase, a <4-minute demo recording) rather than a system intended to run for a real organization.

**Decision:** Optimize for correctness and production-grade *behavior* in the parts that are the point of the project (rate limiting, fallback, circuit breakers, observability). Skip hardening not relevant to that story: no secret rotation, no multi-region HA, no full user-account/RBAC system.

**Consequences:** Meaningfully reduces scope on auth, ops tooling, and deployment topology. Revisit if the gateway is ever repurposed for real traffic.

---

### ADR-003: Scope — Full 6-Phase Plan, Flexible Timeline

**Context:** The spec's 14-day, 6-phase plan (Proxy → Rate Limiting → Resilience → Observability → Testing → Polish) is already sequenced so each phase's output is a prerequisite for testing the next.

**Decision:** Keep the full scope, including priority queues and Ollama integration. Treat day estimates as approximate, not a hard deadline; cut scope later if needed, once it's clear what's actually taking time.

**Consequences:** No pre-emptive scope cuts. Risk is deferred to later in the build rather than eliminated up front.

---

### ADR-004: State Split — Redis for Hot-Path, Postgres for Durable Records

**Context:** Redis is fast but not a natural system of record; the spec's admin API requires durable, queryable history ("all changes are logged with who made them and when"), which Redis alone handles poorly.

**Decision:**
- **Redis:** rate-limit token-bucket counters, circuit-breaker current state, provider health current status, spend running-counter (fast-path enforcement).
- **Postgres:** team configs, per-request spend ledger (source of truth for budget/spend), audit log, circuit-breaker/health history.

Rate-limit counters themselves are never persisted to Postgres — they're meaningless after the fact; historical request-rate data comes from Prometheus/Grafana instead.

**Consequences:** Two datastores to run and reason about, but each is used for what it's actually good at. Spend tracking is dual-write (Redis for the fast check, Postgres as source of truth); on Redis restart, counters can be rebuilt from Postgres.

---

### ADR-005: Config Split — YAML for Global/Static, Postgres for Per-Team/Dynamic

**Context:** The spec's tech-stack table calls for "YAML + hot reload" for no-deploy policy changes, but per-team settings (rate limits, budgets, allowed models) are covered by ADR-004's move to Postgres, edited via the admin API.

**Decision:** YAML owns global, rarely-changed settings: provider endpoints, fallback chains per model tier, circuit-breaker thresholds, health-check intervals, global enrichment defaults. Hot-reloaded on file change. Postgres owns per-team, frequently-changed settings, edited live via the admin API with no restart or file edit.

**Consequences:** Two different config mechanisms for two different audiences — an engineer editing YAML for global routing behavior, and an admin using the API for team-specific limits. Both halves of the original spec's "hot reload" and "no restart" requirements are satisfied by different paths.

---

### ADR-006: Provider Testing Strategy — Mocked OpenAI/Anthropic, Real Ollama

**Context:** Real OpenAI/Anthropic APIs cost money per call, are subject to their own rate limits (which conflicts with testing the gateway's own rate limiting), and can't be forced to fail on command — which is required to reliably demo fallback and circuit-breaker behavior.

**Decision:** Mock OpenAI and Anthropic with configurable fault injection (timeouts, errors, rate-limit responses). Integrate Ollama for real, since it's free and local, to prove the provider-adapter pattern works against a genuinely live API. Real OpenAI/Anthropic keys remain supported via optional env vars for anyone who wants to run against live providers, but nothing in tests/demo depends on them.

**Consequences:** Tests and CI run with no secrets and no cost (see ADR-016). Demo reliably reproduces outages/rate-limits on demand. Trade-off: mocked providers don't prove translation logic against the real APIs' actual response format for OpenAI/Anthropic specifically.

---

### ADR-007: Deployment Topology — Single Instance, Stateless Design

**Context:** The spec's Docker Compose plan describes exactly one gateway service. Whether the gateway could later run as multiple replicas affects where state is allowed to live.

**Decision:** Run one gateway instance for the demo. Design the code so nothing important (rate-limit counts, circuit-breaker state, provider health) lives only in the process's own memory — all of it goes through Redis (ADR-004), which is already the plan regardless of replica count.

**Consequences:** No additional cost for the demo; the system is honestly describable as horizontally-scalable without actually running multiple replicas.

---

### ADR-008: Wire Format — OpenAI-Compatible Schema; Claude as Preferred Provider

**Context:** Each provider has its own request/response schema. The gateway needs one canonical shape that callers use, translated to/from each provider's native format. This is a design/fidelity choice, not a latency one — translation overhead (JSON remapping) is negligible next to LLM network/generation latency regardless of which schema is canonical.

**Decision:** Adopt OpenAI's Chat Completions schema as the standard wire format — it's the de facto industry lingua franca (Ollama already speaks it natively; tools like LiteLLM/OpenRouter use the same standard), minimizing translation code and maximizing legibility to anyone familiar with the space. Separately, treat Anthropic/Claude as the preferred provider in practice: default provider in demo team configs, first in fallback chains, and featured in the dashboards/demo narrative — reflecting real-world usage trends, independent of wire-format choice.

**Consequences:** Anthropic responses require real translation to/from OpenAI shape; Ollama requires almost none. "Preferred provider" is a config/product decision layered on top of the wire-format decision, not in tension with it.

---

### ADR-009: Streaming — Per-Provider Real-Time Translation with Tee-Logging

**Context:** The gateway must present OpenAI-style SSE streaming regardless of provider, while also logging the complete response for observability, without adding noticeable latency.

**Decision:** Translate each provider's native stream chunks into OpenAI-style SSE chunks as they arrive (not after buffering the full response). Simultaneously append each chunk to an in-memory buffer ("tee"); log the fully-assembled response (tokens, cost, latency) once the stream ends.

**Consequences:** This is the most implementation-heavy piece of the proxy layer (flagged as an open risk in the PRD) — three different native stream formats to translate in real time.

---

### ADR-010: Circuit Breaker — Custom, In-Process, Prometheus-Observable

**Context:** The spec hard-requires circuit-breaker state changes to be visible as a Prometheus metric and in a Grafana dashboard panel (both explicit, non-negotiable — one of them is part of the recorded demo). It does not require visibility in the admin API. Off-the-shelf breaker libraries (e.g., `pybreaker`) can satisfy the metrics requirement via listener/callback hooks, so "observability" alone doesn't force a custom build. Per-instance, in-memory circuit breakers are also the standard real-world pattern (e.g., Envoy, resilience4j) — a Redis-shared breaker is not more "correct," just more coordinated.

**Decision:** Build a small custom circuit breaker (closed/open/half-open state machine, ~50-100 lines), in-process and per-instance (not Redis-backed), emitting a Prometheus metric on every state transition, feeding the Grafana Operations dashboard.

**Consequences:** Slightly more code than importing a library, but a stronger interview talking point ("I understand and implemented the pattern"), and avoids unnecessary Redis-coordination complexity that wouldn't reflect standard real-world practice anyway.

**Superseded — Redis-Backed (resilience phase implementation):** The in-process design conflicts with ADR-007's CRITICAL rule (restated in CLAUDE.md) that no important state — explicitly including circuit-breaker state — may live only in a single process's memory. `docs/TRD.md`'s Redis key schema (`breaker:{provider}:state`, `breaker:{provider}:failures`) and `docs/ARCHITECTURE.md`'s State Management section already assumed Redis-backed breaker state, so the in-process choice above was inconsistent with the rest of the design even before this reversal. Circuit breaker state now lives in Redis (this file), atomically transitioned via Lua scripts (same pattern as `gateway/ratelimit/token_bucket.py`), consistent with every other piece of hot-path state in the system.

---

### ADR-011: Tiered Rate Limiting — Threshold Ceilings, Not a Queue

**Context:** The spec wants high-priority (real-time) requests protected from being starved by low-priority (batch) traffic under load. Two implementations were considered: an actual queue that holds low-priority requests until capacity frees up, versus per-tier ceilings within the existing rate limiter.

**Decision:** Give each priority tier its own ceiling within a team's overall rate limit (e.g., real-time traffic can use up to 100% of the limit, batch traffic capped at 60%). Requests that would exceed their tier's ceiling are rejected immediately (429), not queued.

**Consequences:** Reuses the existing Redis token-bucket mechanism with an added per-tier ceiling check — no held connections, no queue-timeout handling, no scheduler to build. Demonstrates the prioritization concept without production-job-scheduler complexity.

---

### ADR-012: Auth — Separate Team-Key and Admin-Token Systems

**Context:** Team callers and admin operators need different, independently-scoped credentials. The spec requires the audit log to record *who* made each admin change — which a single shared admin secret cannot support.

**Decision:** Two independent, opaque-token lookup systems: team API keys (→ team, used for LLM requests) and named admin tokens (→ admin identity, used only on `/admin/*` routes). One person may hold both, as two separate secrets — never a single token encoding both. Tokens are random opaque strings looked up in a table, not self-encoding (e.g., not JWTs) — simpler, and revocation is just deleting a row.

**Consequences:** Satisfies the "who made this change" audit requirement. Slightly more setup (two tables) than a single shared secret, but avoids conflating two different authorization questions in one credential.

---

### ADR-013: Tracing Backend — Add Grafana Tempo

**Context:** The spec's Phase 4.1 requires real distributed tracing spans, but the tech-stack table only lists Prometheus (a metrics store, which cannot store or display individual request traces) and Grafana. As specified, spans would have nowhere to be viewed.

**Decision:** Add Grafana Tempo to the stack — a single additional Docker Compose service that plugs natively into the same Grafana instance already planned.

**Consequences:** Fills a genuine gap in the original spec. One more service to run and wire into the setup script (noted as an open risk in the PRD).

---

### ADR-014: Alerting — Slack Webhook, Env-Gated with Console Fallback

**Context:** The spec wants alerts routed to Slack. A real Slack integration requires a webhook URL from a real Slack workspace — an external setup dependency outside the codebase, awkward for a portfolio demo to require.

**Decision:** Implement a real Slack-webhook alert sink (a simple POST with a JSON payload), configurable via `SLACK_WEBHOOK_URL`. Defaults to logging the alert to console/file when the env var is unset.

**Consequences:** Alerting code is genuinely complete and testable without requiring Slack setup to run the demo; a real "alert pops up in Slack" moment is available on request by setting one env var.

---

### ADR-015: Content Filtering — Rule-Based, Not ML Moderation

**Context:** The spec's "content filters" requirement (Phase 1.4) could mean anything from a keyword blocklist to a full ML moderation-API integration — very different amounts of scope.

**Decision:** Implement simple rule-based (keyword/regex) filtering, configurable per team.

**Consequences:** Proves the architectural point (centralized, per-team-configurable policy enforcement) without turning into a separate content-moderation project.

---

### ADR-016: CI — GitHub Actions Running the Test Suite

**Context:** Not specified in the original brief, but low-cost given ADR-006 (mocked providers mean the test suite needs no real API keys or secrets to run).

**Decision:** Add a GitHub Actions workflow running `pytest` (and linting) on every push.

**Consequences:** A visible, low-effort signal of engineering maturity (green CI badge) for the portfolio repo.

---

### ADR-017: Ollama — Adapter Built Now, Live Verification Deferred

**Context:** Ollama isn't installed on the development machine at plan time. Harness steps run autonomously (`claude -p --dangerously-skip-permissions`, `scripts/execute.py`), so a step whose acceptance criteria requires an actual live call to a missing Ollama server would fail or block with no human present to install it.

**Decision:** Write the real `OllamaAdapter` (translation to/from Ollama's native API) as part of the automated build. Keep any autonomous step's acceptance criteria to unit-level verification (adapter logic against a stubbed HTTP response), not a live call. Live-call verification against a real running Ollama server is a manual step performed once Ollama is installed locally.

**Consequences:** The adapter code is genuinely complete and structurally identical to the OpenAI/Anthropic adapters. The "prove the pattern against a real live API" story (ADR-006) is validated manually later, not by the automated pipeline.

---

### ADR-018: Token Pricing Table — YAML, Real List Prices

**Context:** Budget enforcement (`cost = input_tokens × input_price + output_tokens × output_price`) requires a $/token table per model. Neither the Postgres schema nor the YAML sample defined one — a genuine gap between the spec's requirements and the data model.

**Decision:** Add a `pricing` section to the global YAML config (per-model input/output $ per token), populated with real published OpenAI/Anthropic/Ollama list prices. This is product logic (spend tracking and budget dashboards are deliverables), not a testing concern — mocking providers avoids being billed, but the gateway still needs a plausible cost figure to enforce budgets against and to populate the Business dashboard.

**Consequences:** Pricing is global and rarely changed, fitting the same YAML bucket as fallback chains and circuit-breaker thresholds (ADR-005). No Postgres schema change needed.

---

### ADR-019: Token Counting — Fabricated in Mocks, Native via Ollama

**Context:** Cost calculation needs input/output token counts per request. Mocked OpenAI/Anthropic providers have no real model behind them to report real counts; Ollama does.

**Decision:** Mock servers return a plausible fabricated `usage` object (`prompt_tokens`/`completion_tokens`) rather than running a real tokenizer. `OllamaAdapter` translates Ollama's native usage fields (`prompt_eval_count`, `eval_count`) into the canonical OpenAI-style `usage` object.

**Consequences:** Avoids adding a tokenizer dependency (e.g. `tiktoken`) purely to count fake tokens. Ollama's real usage numbers keep that adapter's cost and observability data genuinely accurate.

---

### ADR-020: Priority Tier Signaling — `X-Priority` Header

**Context:** ADR-011's tiered rate limiting needs each request to carry a priority tier (`realtime`/`batch`), but the canonical wire format is strictly OpenAI's Chat Completions schema (ADR-008), which has no field for it.

**Decision:** Signal priority via a custom HTTP header, `X-Priority: realtime|batch`, defaulting to `realtime` when absent. The request body stays byte-for-byte OpenAI-compatible.

**Consequences:** Keeps ADR-008's compatibility guarantee intact. The rate-limit-check step (TRD §3) reads this header alongside `Authorization`.

---

### ADR-021: Per-Team Enrichment/Content-Filter Storage — `teams.config jsonb`

**Context:** The YAML's `enrichment_defaults` section notes "per-team overrides live in Postgres," but the `teams` table (TRD §4.1) had no column for system-prompt/disclaimer/content-filter overrides.

**Decision:** Add a `config jsonb` column to the `teams` table holding each team's enrichment and content-filter overrides.

**Consequences:** One extra column, no new table or join — read together with the rest of a team's config (allowed models, limits) on every request.

---

### ADR-022: CI Integration Test Infrastructure — Real Redis/Postgres Service Containers

**Context:** ADR-016 established GitHub Actions running `pytest`, but not whether the concurrent-load rate-limiting, budget, and circuit-breaker integration tests run against real datastores or in-memory substitutes (`fakeredis`, SQLite).

**Decision:** Use real Redis and Postgres as GitHub Actions service containers alongside the test job.

**Consequences:** Tests exercise the actual Lua/SQL the gateway runs in production, not an approximation. No new secrets or cost — service containers are free on GitHub-hosted runners.

---

### ADR-023: Python Packaging — `uv`

**Context:** No `pyproject.toml`/`requirements.txt` existed at plan time. `uv` and `pip3` are available on the dev machine; `poetry` is not.

**Decision:** Use `uv` for dependency management, the virtual environment, and the lockfile.

**Consequences:** Single lockfile, fast installs; `scripts/setup_demo_teams.py` and CI both run the project via `uv run`.

---

### ADR-024: Harness Phase Breakdown — 5 Automated Phases, Polish Manual

**Context:** The PRD's 6-phase build guide doesn't map 1:1 onto Harness's model of one phase = one branch = one sequence of autonomous steps (`scripts/execute.py`, `.claude/commands/harness.md`). Phase 6 ("Polish": demo recording, interview narrative) isn't code an autonomous step can produce.

**Decision:** Split the build into 5 Harness phases, each its own `feat-{phase}` branch:

1. `proxy-layer` — PRD Phase 1 (provider abstraction, auth/routing, streaming passthrough, enrichment; project setup is step 0 within this phase)
2. `ratelimit-budget` — PRD Phase 2 (token buckets, budget caps, tiered limits, admin API)
3. `resilience` — PRD Phase 3 (health checks, fallback routing, retry/backoff, circuit breakers)
4. `observability` — PRD Phase 4 (OTel spans, Prometheus metrics, Grafana dashboards, alerting)
5. `test-load` — PRD Phase 5 (integration test suite, Locust load test, full Docker Compose stack)

PRD Phase 6 (demo recording + narrative) is done manually and is not encoded as a Harness phase.

**Consequences:** Each phase gets its own review checkpoint (branch/PR) before the next begins; later phases' steps can read earlier phases' `summary` fields as context automatically.

---

### ADR-025: Mock Fault Injection — Stateless, Per-Request Trigger

**Context:** ADR-006's mocks need "configurable fault injection" to demo/test outages, fallback, and circuit-breaker behavior. An env-var or shared-control-endpoint approach is global mutable state, awkward when tests or the load test (5,000+ concurrent requests) want different simulated behavior on different requests at the same time.

**Decision:** Mocks read a per-request trigger — a `X-Mock-Fault: timeout|error|rate_limit` header or magic model name — and respond accordingly, statelessly, with no shared mutable state between requests.

**Consequences:** Concurrent tests and load-test traffic can each control their own simulated fault independently, with no race conditions over shared toggle state.

---

## Implementation Notes (Not Formal Decisions)

Lower-stakes choices settled as working assumptions rather than dedicated ADRs:
- **Load testing tool:** Locust (Python-native).
- **Retry/backoff:** standard exponential backoff, e.g. the `tenacity` library.
- **Health-check loop:** an asyncio background task within the single gateway process (per ADR-007).
- **Package/dependency manager:** `uv` (ADR-023).
