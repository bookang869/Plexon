"""Named threshold constants for phases/benchmarks steps 1-4. Values are
calibrated for a single-process gateway running via Docker Compose on a
developer laptop (ADR-002: portfolio/demo rigor, not a tuned production
deployment) -- looser than TRD §10's headline targets where a laptop-scale
Docker stack can't realistically hit them, but still meaningful as a
regression signal.
"""

from __future__ import annotations

# Gateway-overhead benchmark (step 1) -----------------------------------------

# TRD §10 targets <10ms end-to-end overhead; read off gateway_overhead_seconds
# (gateway-only time, excluding the mocked-provider round trip) via /metrics.
# Doubled from the TRD's headline number to leave room for Docker Compose
# networking/venv overhead on a dev machine while still catching real regressions.
OVERHEAD_P95_MS = 20.0

# Throughput benchmark (step 2) -----------------------------------------------

# Sustained requests/sec the gateway must clear against mocked providers on a
# single dev-machine instance -- not a claim about the <10ms/5,000-concurrent
# NFR, just a floor to catch a throughput regression between runs.
THROUGHPUT_MIN_RPS = 50.0

# Client-observed p95 latency (ms) under the throughput benchmark's sustained
# load -- looser than OVERHEAD_P95_MS since it includes full round-trip time
# (network + mocked-provider call), not gateway-only time.
THROUGHPUT_P95_MS = 200.0

# Failover benchmark (step 3) -------------------------------------------------

# Percentage of requests that must still succeed (via fallback) while the
# primary provider is failing -- ADR-011/PRD Core Feature 3's fallback-chain
# guarantee; not 100% since a handful of in-flight requests can legitimately
# race the failure/circuit-breaker transition.
FAILOVER_RELIABILITY_MIN_PCT = 95.0

# Max wall-clock seconds from the first injected fault to the first request
# that's actually served by the fallback provider -- bounded by the retry
# budget (tenacity, up to 3 attempts w/ exponential backoff) rather than an
# external SLA figure.
FAILOVER_SWITCH_MAX_SECONDS = 5.0

# Max seconds the circuit breaker may take to close again after its
# configured cooldown_seconds elapses (deploy config, TRD §5) -- allows for
# one health-check-interval's worth of scheduling slack on top of the
# cooldown itself, not an instantaneous transition.
RECOVERY_MAX_SECONDS_OVER_COOLDOWN = 10.0

# Rate-limit / budget benchmark (step 4) --------------------------------------

# Allowed deviation (as a fraction, e.g. 0.1 = 10%) between a team's
# configured rpm/tpm ceiling and the number of requests actually admitted
# under concurrent load -- Redis token-bucket admission is atomic per
# request, but wall-clock refill timing during a live benchmark run (unlike
# the deterministic tests/test_token_bucket.py) introduces some slack.
RATELIMIT_ADMIT_TOLERANCE = 0.1

# Max percentage a team's recorded spend may exceed its configured budget
# before the hard block takes effect -- bounded by the cost of the single
# in-flight request that pushed spend over 100%, not an ongoing leak.
BUDGET_OVERSHOOT_MAX_PCT = 5.0

# Max p95 client-observed latency (ms) for *rejected* (429) responses under
# the rpm admission-accuracy benchmark -- a rejection is just auth plus a
# single Redis EVAL (gateway/ratelimit/token_bucket.py), no provider call and
# no spend_ledger write, so its own work is trivial. In practice the
# dominant cost is Postgres connection-pool contention: every request --
# admitted or not -- needs a pool connection for the auth lookup
# (gateway/auth/team_auth.py), and asyncpg's default pool (gateway/db.py) is
# only 10 connections, so a few-hundred-request burst queues meaningfully
# for a slot behind admitted requests' longer-held connections. Measured
# p95 ~1.4s / p99 ~1.5s at 200 concurrent requests against a 30 rpm_limit on
# a dev-machine Docker Compose stack; this constant leaves real headroom
# above that baseline rather than asserting rejections are instant.
RATELIMIT_REJECTION_P95_MAX_MS = 2500.0
