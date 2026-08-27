"""Generic continuous-refill token bucket against Redis (ADR-004, ADR-007,
ADR-011). Team/tier-agnostic primitive -- callers own the bucket key naming
and business rules (see TRD SS4.2 for the `ratelimit:{team_id}:{tier}:*`
convention used by callers of this module).
"""

from __future__ import annotations

from pydantic import BaseModel
from redis.asyncio import Redis

# Reads current tokens/ts from a Redis hash, applies continuous refill up to
# `capacity`, and deducts `cost` if enough tokens are available -- all inside
# one EVAL so concurrent callers against the same key can't interleave
# read-compute-write. Uses Redis's own TIME (not a Python-supplied timestamp)
# so refill math never depends on app/Redis clock skew. Dividing by
# refill_per_second == 0 yields Lua's IEEE-754 `inf`, which parses cleanly as
# Python's float("inf") -- no special-casing needed for a zero refill rate.
_CHECK_AND_CONSUME_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_second = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl_seconds = tonumber(ARGV[4])

local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens

if bucket[1] == false then
    tokens = capacity
else
    local elapsed = now - tonumber(bucket[2])
    tokens = math.min(capacity, tonumber(bucket[1]) + elapsed * refill_per_second)
end

local allowed
local retry_after

if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
    retry_after = 0
else
    allowed = 0
    retry_after = (cost - tokens) / refill_per_second
end

redis.call('HMSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', key, ttl_seconds)

return {allowed, tostring(tokens), tostring(retry_after)}
"""

# Bounded add with no refill-rate/time computation -- creates the bucket
# (recording `ts` via Redis TIME) if it doesn't exist yet, since a later
# check_and_consume call requires both hash fields to be present.
_REFUND_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])

local existing = redis.call('HGET', key, 'tokens')
local tokens

if existing == false then
    local time = redis.call('TIME')
    local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
    tokens = math.min(capacity, amount)
    redis.call('HMSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
else
    tokens = math.min(capacity, tonumber(existing) + amount)
    redis.call('HSET', key, 'tokens', tostring(tokens))
end

return tostring(tokens)
"""


class BucketResult(BaseModel):
    allowed: bool
    remaining: float
    retry_after_seconds: float | None


async def check_and_consume(
    redis: Redis,
    key: str,
    capacity: float,
    refill_per_second: float,
    cost: float = 1.0,
    ttl_seconds: int = 120,
) -> BucketResult:
    script = redis.register_script(_CHECK_AND_CONSUME_SCRIPT)
    allowed, remaining, retry_after = await script(
        keys=[key], args=[capacity, refill_per_second, cost, ttl_seconds]
    )
    allowed = bool(int(allowed))
    return BucketResult(
        allowed=allowed,
        remaining=float(remaining),
        retry_after_seconds=None if allowed else float(retry_after),
    )


async def refund(redis: Redis, key: str, capacity: float, amount: float) -> None:
    script = redis.register_script(_REFUND_SCRIPT)
    await script(keys=[key], args=[capacity, amount])
