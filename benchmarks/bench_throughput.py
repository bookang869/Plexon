"""Sustained-throughput benchmark (step 2 of phases/benchmarks; TRD §10).

Not a substitute for tests/load/locustfile.py: Locust drives thousands of
real OS-level concurrent users from outside this Python process against the
live docker-compose stack, exercising TRD §10's "5,000+ concurrent requests"
NFR. This benchmark instead runs tens-to-low-hundreds of `asyncio` tasks
inside a single pytest process -- a much smaller-scale, faster regression
signal for the gateway's own request-handling throughput (auth, rate-limit
check, budget check, DB/Redis round trips) against mock-openai's near-instant
responses, not a claim about the 5,000+ concurrent target.

Concurrency and duration are overridable via PLEXON_BENCHMARK_CONCURRENCY and
PLEXON_BENCHMARK_DURATION_SECONDS env vars; the defaults below are small
smoke-test values, same convention as locustfile.py's "for a quick smoke
run... use much smaller -u/-r" note -- bump both via env for a heavier run.
"""

from __future__ import annotations

import os
import time

import pytest

from benchmarks import thresholds
from benchmarks.common import (
    BenchmarkResult,
    LatencySample,
    gateway_client,
    percentiles,
    run_concurrent,
    write_result,
)

_MODEL = "gpt-4o-mini"
_CONCURRENCY = int(os.environ.get("PLEXON_BENCHMARK_CONCURRENCY", "10"))
_DURATION_SECONDS = float(os.environ.get("PLEXON_BENCHMARK_DURATION_SECONDS", "5"))


def _payload() -> dict:
    return {"model": _MODEL, "messages": [{"role": "user", "content": "benchmark throughput"}]}


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_sustained_throughput(benchmark_team):
    client = await gateway_client()
    headers = {"Authorization": f"Bearer {benchmark_team['api_key']}"}
    try:

        async def _make_request() -> LatencySample:
            started = time.monotonic()
            resp = await client.post("/v1/chat/completions", json=_payload(), headers=headers)
            return LatencySample(
                started_at=started,
                elapsed_seconds=time.monotonic() - started,
                success=resp.status_code == 200,
            )

        wall_start = time.monotonic()
        samples = await run_concurrent(
            _make_request, concurrency=_CONCURRENCY, duration_seconds=_DURATION_SECONDS
        )
        wall_elapsed_seconds = time.monotonic() - wall_start
    finally:
        await client.aclose()

    failures = [s for s in samples if not s.success]
    achieved_rps = (len(samples) - len(failures)) / wall_elapsed_seconds
    error_rate = len(failures) / len(samples) if samples else 0.0

    quantiles = percentiles([s.elapsed_seconds for s in samples])

    result = BenchmarkResult(
        name="throughput",
        metrics={
            "achieved_rps": achieved_rps,
            "total_requests": len(samples),
            "error_count": len(failures),
            "error_rate": error_rate,
            "p50_ms": quantiles[0.5] * 1000,
            "p95_ms": quantiles[0.95] * 1000,
            "p99_ms": quantiles[0.99] * 1000,
        },
        thresholds={
            "min_rps": thresholds.THROUGHPUT_MIN_RPS,
            "p95_ms": thresholds.THROUGHPUT_P95_MS,
        },
        passed=achieved_rps >= thresholds.THROUGHPUT_MIN_RPS and error_rate == 0.0,
    )
    write_result(result)

    assert achieved_rps >= thresholds.THROUGHPUT_MIN_RPS
    assert error_rate == 0.0
