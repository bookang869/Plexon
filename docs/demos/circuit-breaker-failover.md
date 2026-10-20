# Demo: Primary Failure → Circuit Breaker → Fallback → Recovery

A real, runnable transcript of Plexon's resilience path end to end: a request routed to the primary provider, the primary failing, the circuit breaker opening, the fallback provider serving traffic, and the breaker recovering after cooldown.

**Script:** [`scripts/demo_circuit_breaker_failover.py`](../../scripts/demo_circuit_breaker_failover.py) — hits a running gateway over plain HTTP only (no direct Redis/Postgres access), so it sees exactly what a real caller sees. Faults are injected via the mock-anthropic fault-injection magic suffix (ADR-025): `claude-sonnet--fault-error` always returns an instant HTTP 500 from `mock-anthropic`, classified as a retryable provider error — the same mechanism `benchmarks/bench_failover.py` uses.

**Run it yourself:**

```bash
docker compose -f deploy/docker-compose.yml up -d --build
python3 scripts/setup_demo_teams.py
uv run python3 scripts/demo_circuit_breaker_failover.py
```

## Transcript

Captured against the local Docker Compose stack (Apple M4, macOS, mock OpenAI/Anthropic providers — ADR-006). `served_by` is the `model` field in the gateway's response, which reveals which provider actually served the request: `claude-sonnet` only succeeds on `anthropic`; `gpt-4o-mini` is this model's fallback on `openai` (`fast_tier` chain, `config.yaml`).

```
=== 1. Healthy baseline: primary provider (anthropic) serves the request ===
POST /v1/chat/completions model='claude-sonnet' -> 200, served_by='claude-sonnet', 27ms

Breaker before fault injection: state=closed (closed->open transitions so far: 7)

=== 2. Injecting faults: 7 requests to model='claude-sonnet--fault-error' ===
(mock-anthropic returns an instant 500 for this model; gateway retries
the primary up to 3x with backoff, then falls back -- once 5 failures land within the window, the breaker opens
and later requests skip retrying the primary entirely)
  request 0: -> 200, served_by='gpt-4o-mini', 336ms
  request 1: -> 200, served_by='gpt-4o-mini', 318ms
  request 2: -> 200, served_by='gpt-4o-mini', 324ms
  request 3: -> 200, served_by='gpt-4o-mini', 315ms
  request 4: -> 200, served_by='gpt-4o-mini', 315ms
  request 5: -> 200, served_by='gpt-4o-mini', 5ms
  request 6: -> 200, served_by='gpt-4o-mini', 5ms

Breaker after fault injection: state=open (closed->open transitions: 8, fallback activations anthropic->openai: 118 -> 125)

=== 3. One more request while breaker is open (should skip the primary, fast) ===
POST /v1/chat/completions model='claude-sonnet--fault-error' -> 200, served_by='gpt-4o-mini', 5ms

=== 4. Waiting 30s cooldown, then probing with a healthy request ===
POST /v1/chat/completions model='claude-sonnet' -> 200, served_by='claude-sonnet', 30ms

Breaker after recovery probe: state=closed (open->half_open: 3, half_open->closed: 3)

=== Done: primary failed, breaker opened, fallback served traffic, breaker recovered. ===
```

## Reading the evidence

- **Zero dropped requests.** Every single request in the transcript returns `200` — callers never see the primary's failure; they just get served by whichever provider is actually healthy.
- **The retry cost is visible and then disappears.** Requests 0–4 cost ~315–336ms each — the gateway retrying the primary provider up to 3x with exponential backoff before falling back (the `CLAUDE.md` CRITICAL retry rule). Once the breaker opens at request 5, the cost drops to ~5ms: the gateway now skips the known-bad primary and goes straight to fallback.
- **The breaker opened exactly where expected.** `config.yaml`'s `circuit_breaker.failure_threshold: 5` — the state flips to `open` once the 5th failure lands inside the window, confirmed independently by the `gateway_circuit_breaker_state` Prometheus gauge (`/metrics`) going from `0` (closed) to `2` (open).
- **Recovery is automatic.** After the `cooldown_seconds: 30` window, a single healthy probe is enough to move the breaker `open → half_open → closed` (both transitions counted in `gateway_circuit_breaker_transitions_total`), and the very next `claude-sonnet` request is served by `anthropic` again — no manual intervention.
