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

This module sweeps concurrency across PLEXON_BENCHMARK_SWEEP_CONCURRENCIES
(default 5, 20, 50, 100, 200) rather than running at a single fixed level, so
the benchmark reports how throughput scales with concurrency and what the
maximum concurrency is that the gateway can sustain with zero errors.
"""

from __future__ import annotations

import os
import time

import httpx
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
_SWEEP_CONCURRENCIES = [
    int(c) for c in os.environ.get("PLEXON_BENCHMARK_SWEEP_CONCURRENCIES", "5,20,50,100,200").split(",")
]

_sweep_results: list[dict] = []


def _payload() -> dict:
    return {"model": _MODEL, "messages": [{"role": "user", "content": "benchmark throughput"}]}


async def _run_at_concurrency(
    client: httpx.AsyncClient, headers: dict, concurrency: int, duration_seconds: float
) -> dict:
    async def _make_request() -> LatencySample:
        started = time.monotonic()
        resp = await client.post("/v1/chat/completions", json=_payload(), headers=headers)
        return LatencySample(
            started_at=started,
            elapsed_seconds=time.monotonic() - started,
            success=resp.status_code == 200,
        )

    wall_start = time.monotonic()
    samples = await run_concurrent(_make_request, concurrency=concurrency, duration_seconds=duration_seconds)
    wall_elapsed_seconds = time.monotonic() - wall_start

    failures = [s for s in samples if not s.success]
    achieved_rps = (len(samples) - len(failures)) / wall_elapsed_seconds
    error_rate = len(failures) / len(samples) if samples else 0.0

    quantiles = percentiles([s.elapsed_seconds for s in samples])

    return {
        "concurrency": concurrency,
        "achieved_rps": achieved_rps,
        "total_requests": len(samples),
        "error_count": len(failures),
        "error_rate": error_rate,
        "p50_ms": quantiles[0.5] * 1000,
        "p95_ms": quantiles[0.95] * 1000,
        "p99_ms": quantiles[0.99] * 1000,
    }


@pytest.mark.benchmark
@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", _SWEEP_CONCURRENCIES)
async def test_throughput_sweep(concurrency, benchmark_team):
    client = await gateway_client()
    headers = {"Authorization": f"Bearer {benchmark_team['api_key']}"}
    try:
        metrics = await _run_at_concurrency(client, headers, concurrency, _DURATION_SECONDS)
    finally:
        await client.aclose()

    result = BenchmarkResult(
        name=f"throughput_c{concurrency}",
        metrics=metrics,
        thresholds={
            "min_rps": thresholds.THROUGHPUT_MIN_RPS,
            "p95_ms": thresholds.THROUGHPUT_P95_MS,
        },
        passed=metrics["achieved_rps"] >= thresholds.THROUGHPUT_MIN_RPS and metrics["error_rate"] == 0.0,
    )
    write_result(result)

    _sweep_results.append(metrics)


@pytest.mark.benchmark
def test_throughput_sweep_summary():
    clean_levels = [level for level in _sweep_results if level["error_rate"] == 0.0]
    if clean_levels:
        best = max(clean_levels, key=lambda level: level["concurrency"])
        max_sustainable_concurrency = best["concurrency"]
        max_sustainable_rps = best["achieved_rps"]
    else:
        max_sustainable_concurrency = None
        max_sustainable_rps = None

    result = BenchmarkResult(
        name="throughput_sweep_summary",
        metrics={
            "max_sustainable_concurrency": max_sustainable_concurrency,
            "max_sustainable_rps": max_sustainable_rps,
            "levels": _sweep_results,
        },
        thresholds={"min_rps": thresholds.THROUGHPUT_MIN_RPS},
        passed=max_sustainable_rps is not None and max_sustainable_rps >= thresholds.THROUGHPUT_MIN_RPS,
    )
    write_result(result)

    assert max_sustainable_rps is not None and max_sustainable_rps >= thresholds.THROUGHPUT_MIN_RPS
