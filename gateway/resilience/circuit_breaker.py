"""Generic, provider-agnostic circuit-breaker state machine (ADR-004, ADR-007,
ADR-010 superseded). Redis-backed, not in-process, so breaker state survives
past any single gateway process the same way rate-limit/spend state does.
State transitions follow the same atomic-Lua-script pattern as
`gateway/ratelimit/token_bucket.py` -- no read-then-write TOCTOU gap under
concurrent callers, no clock skew between app and Redis (uses Redis TIME).

This module has no concept of "retryable" vs "non-retryable" errors and does
not import from `gateway/providers/errors.py` -- callers decide what counts
as a failure worth reporting; this module just records what it's told.
"""

from __future__ import annotations

import logging
from enum import Enum

from pydantic import BaseModel
from redis.asyncio import Redis

from gateway.config.loader import CircuitBreakerConfig
from gateway.db import get_pool
from gateway.observability.metrics import (
    gateway_circuit_breaker_state,
    gateway_circuit_breaker_transitions_total,
)

logger = logging.getLogger(__name__)

# TRD §8: 0=closed, 1=half_open, 2=open.
_STATE_TO_GAUGE_VALUE = {
    "closed": 0,
    "half_open": 1,
    "open": 2,
}

# TTL for the half-open probe claim marker -- comfortably longer than a
# single provider call could plausibly take, so an abandoned probe (the
# claiming caller crashed before calling record_success/record_failure)
# doesn't permanently wedge the breaker in half-open.
_PROBE_CLAIMED_TTL_SECONDS = 60

# check_breaker: reads current state, and if open with cooldown elapsed,
# atomically claims the single half-open probe slot for exactly one caller.
# Redis executes this whole script to completion with no other command (or
# other invocation of this same script) interleaved -- that atomicity is what
# guarantees a single winner across concurrent callers, the same reasoning
# token_bucket.py relies on for check_and_consume.
_CHECK_BREAKER_SCRIPT = """
local state_key = KEYS[1]
local opened_at_key = KEYS[2]
local probe_claimed_key = KEYS[3]
local cooldown_seconds = tonumber(ARGV[1])
local probe_ttl_seconds = tonumber(ARGV[2])

local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

local state = redis.call('GET', state_key)
if state == false then
    state = 'closed'
end

if state == 'closed' then
    return {1, 0, 0}
end

if state == 'half_open' then
    local claimed = redis.call('EXISTS', probe_claimed_key)
    if claimed == 1 then
        return {0, 0, 0}
    end
    -- Claiming caller crashed without resolving the probe -- reclaim the
    -- slot. No state change (still half_open), so no history row.
    redis.call('SET', probe_claimed_key, '1', 'EX', probe_ttl_seconds)
    return {1, 1, 0}
end

-- state == 'open'
local opened_at = redis.call('GET', opened_at_key)
local elapsed
if opened_at == false then
    elapsed = cooldown_seconds
else
    elapsed = now - tonumber(opened_at)
end

if elapsed >= cooldown_seconds then
    redis.call('SET', state_key, 'half_open')
    redis.call('SET', probe_claimed_key, '1', 'EX', probe_ttl_seconds)
    return {1, 1, 1}
end

return {0, 0, 0}
"""

# Non-probe failure: increments the failure counter (refreshing its TTL to
# window_seconds so failures outside the window stop counting), and opens
# the breaker only once the count reaches failure_threshold.
_RECORD_FAILURE_SCRIPT = """
local failures_key = KEYS[1]
local state_key = KEYS[2]
local opened_at_key = KEYS[3]
local window_seconds = tonumber(ARGV[1])
local failure_threshold = tonumber(ARGV[2])

local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

local count = redis.call('INCR', failures_key)
redis.call('EXPIRE', failures_key, window_seconds)

if count < failure_threshold then
    return 0
end

local state = redis.call('GET', state_key)
if state == 'open' then
    return 0
end

redis.call('SET', state_key, 'open')
redis.call('SET', opened_at_key, tostring(now))
return 1
"""

# A failed probe is decisive on its own -- reopen immediately, independent of
# failure_threshold, and reset opened_at so cooldown timing restarts fresh.
_RECORD_PROBE_FAILURE_SCRIPT = """
local state_key = KEYS[1]
local opened_at_key = KEYS[2]
local probe_claimed_key = KEYS[3]

local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

redis.call('SET', state_key, 'open')
redis.call('SET', opened_at_key, tostring(now))
redis.call('DEL', probe_claimed_key)
return 1
"""

# A successful probe closes the breaker immediately and resets the failure
# counter, so the next failure starts counting toward threshold from zero.
_RECORD_PROBE_SUCCESS_SCRIPT = """
local state_key = KEYS[1]
local failures_key = KEYS[2]
local probe_claimed_key = KEYS[3]

redis.call('SET', state_key, 'closed')
redis.call('DEL', failures_key)
redis.call('DEL', probe_claimed_key)
return 1
"""

_INSERT_HISTORY_ROW = """
    INSERT INTO circuit_breaker_history (provider, from_state, to_state, reason)
    VALUES ($1, $2, $3, $4)
"""


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BreakerDecision(BaseModel):
    allowed: bool
    is_probe: bool


def _state_key(provider: str) -> str:
    return f"breaker:{provider}:state"


def _failures_key(provider: str) -> str:
    return f"breaker:{provider}:failures"


def _opened_at_key(provider: str) -> str:
    return f"breaker:{provider}:opened_at"


def _probe_claimed_key(provider: str) -> str:
    return f"breaker:{provider}:probe_claimed"


async def _record_transition(
    provider: str, from_state: BreakerState, to_state: BreakerState, reason: str
) -> None:
    """Best-effort history write (ADR-004: Postgres is durable history, not
    the hot-path system of record) -- mirrors `gateway/ratelimit/budget.py`'s
    `record_spend` pattern. Never raises, never blocks a breaker decision on
    Postgres being reachable. The Prometheus metrics below run unconditionally
    -- they must never depend on the best-effort Postgres write succeeding.
    """
    gateway_circuit_breaker_transitions_total.labels(
        provider=provider, from_state=from_state.value, to_state=to_state.value
    ).inc()
    gateway_circuit_breaker_state.labels(provider=provider).set(_STATE_TO_GAUGE_VALUE[to_state.value])

    try:
        await get_pool().execute(_INSERT_HISTORY_ROW, provider, from_state.value, to_state.value, reason)
    except Exception:
        logger.exception(
            "failed to write circuit_breaker_history row for provider=%s %s->%s",
            provider,
            from_state.value,
            to_state.value,
        )


async def check_breaker(redis: Redis, provider: str, config: CircuitBreakerConfig) -> BreakerDecision:
    script = redis.register_script(_CHECK_BREAKER_SCRIPT)
    allowed, is_probe, transitioned = await script(
        keys=[_state_key(provider), _opened_at_key(provider), _probe_claimed_key(provider)],
        args=[config.cooldown_seconds, _PROBE_CLAIMED_TTL_SECONDS],
    )
    if int(transitioned):
        await _record_transition(provider, BreakerState.OPEN, BreakerState.HALF_OPEN, "cooldown_elapsed")
    return BreakerDecision(allowed=bool(int(allowed)), is_probe=bool(int(is_probe)))


async def record_success(redis: Redis, provider: str, was_probe: bool) -> None:
    if not was_probe:
        return
    script = redis.register_script(_RECORD_PROBE_SUCCESS_SCRIPT)
    await script(keys=[_state_key(provider), _failures_key(provider), _probe_claimed_key(provider)])
    await _record_transition(provider, BreakerState.HALF_OPEN, BreakerState.CLOSED, "probe_succeeded")


async def record_failure(
    redis: Redis, provider: str, was_probe: bool, config: CircuitBreakerConfig
) -> None:
    if was_probe:
        script = redis.register_script(_RECORD_PROBE_FAILURE_SCRIPT)
        await script(
            keys=[_state_key(provider), _opened_at_key(provider), _probe_claimed_key(provider)]
        )
        await _record_transition(provider, BreakerState.HALF_OPEN, BreakerState.OPEN, "probe_failed")
        return

    script = redis.register_script(_RECORD_FAILURE_SCRIPT)
    transitioned = await script(
        keys=[_failures_key(provider), _state_key(provider), _opened_at_key(provider)],
        args=[config.window_seconds, config.failure_threshold],
    )
    if int(transitioned):
        await _record_transition(
            provider, BreakerState.CLOSED, BreakerState.OPEN, "failure_threshold_reached"
        )
