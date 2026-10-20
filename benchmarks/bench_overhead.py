"""Gateway-overhead benchmark (step 1 of phases/benchmarks; TRD §8/§10,
ADR-026). Validates `gateway_overhead_seconds` as reported by the live
`/metrics` endpoint -- requires the full docker-compose stack including the
gateway container itself (see benchmarks/common.py's gateway_client()); an
in-process ASGITransport client wouldn't be observing the same process the
real running gateway serves metrics from.

Sweeps concurrency across PLEXON_BENCHMARK_SWEEP_CONCURRENCIES (default 5,
20, 50, 100, 200), same env var and default as bench_throughput.py's sweep,
so overhead-under-load is visible alongside throughput-under-load. Only the
concurrency=5 level hard-asserts thresholds.OVERHEAD_P95_MS -- that's the
level the threshold was actually calibrated against (see thresholds.py's
comment); other levels still compute and report a `passed` field for
visibility but don't fail the test.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from benchmarks import thresholds
from benchmarks.common import (
    BenchmarkResult,
    LatencySample,
    gateway_client,
    histogram_percentiles_from_metrics_text,
    run_concurrent,
    write_result,
)

_MODEL = "gpt-4o-mini"
_WARMUP_REQUESTS = 10
_TOTAL_REQUESTS = 200
_CALIBRATED_CONCURRENCY = 5
_SWEEP_CONCURRENCIES = [
    int(c) for c in os.environ.get("PLEXON_BENCHMARK_SWEEP_CONCURRENCIES", "5,20,50,100,200").split(",")
]

_METRIC_NAME = "gateway_overhead_seconds"
_LABELS = {"route": "chat_completion"}

_sweep_results: list[dict] = []


def _payload() -> dict:
    return {"model": _MODEL, "messages": [{"role": "user", "content": "benchmark overhead"}]}


def _bucket_counts(metrics_text: str) -> dict[float, float]:
    counts: dict[float, float] = {}
    for family in text_string_to_metric_families(metrics_text):
        if family.name != _METRIC_NAME:
            continue
        for sample in family.samples:
            if not sample.name.endswith("_bucket"):
                continue
            if any(sample.labels.get(k) != v for k, v in _LABELS.items()):
                continue
            counts[float(sample.labels["le"])] = sample.value
    return counts


def _delta_histogram_text(before_text: str, after_text: str) -> str:
    """`gateway_overhead_seconds` is a process-lifetime cumulative Prometheus
    histogram shared by every request the live gateway container has ever
    served (including prior benchmark/demo runs against the same
    long-lived container) -- reading /metrics once after this benchmark's
    own load would mix in that unrelated history. Snapshotting before and
    after and diffing each `le` bucket isolates just this run's own
    requests, the same before/after idiom tests/test_metrics.py uses for
    counters. Re-synthesizes a minimal exposition-format text block so the
    diffed buckets can still go through common.py's histogram_percentiles_
    from_metrics_text unchanged.
    """
    before = _bucket_counts(before_text)
    after = _bucket_counts(after_text)
    label_str = ",".join(f'{k}="{v}"' for k, v in _LABELS.items())
    lines = [f"# TYPE {_METRIC_NAME} histogram"]
    for le in sorted(after):
        delta = after[le] - before.get(le, 0.0)
        lines.append(f'{_METRIC_NAME}_bucket{{{label_str},le="{le}"}} {delta}')
    return "\n".join(lines)


async def _measure_overhead_at_concurrency(client: httpx.AsyncClient, headers: dict, concurrency: int) -> dict:
    for _ in range(_WARMUP_REQUESTS):
        await client.post("/v1/chat/completions", json=_payload(), headers=headers)

    before_resp = await client.get("/metrics", follow_redirects=True)
    before_resp.raise_for_status()

    async def _make_request() -> LatencySample:
        started = time.monotonic()
        resp = await client.post("/v1/chat/completions", json=_payload(), headers=headers)
        return LatencySample(
            started_at=started,
            elapsed_seconds=time.monotonic() - started,
            success=resp.status_code == 200,
        )

    # Scale sample count with concurrency so higher levels still get enough
    # requests for a meaningful percentile estimate, not just _TOTAL_REQUESTS
    # spread thinner across more workers.
    total_requests = max(_TOTAL_REQUESTS, concurrency * 10)
    samples = await run_concurrent(_make_request, concurrency=concurrency, total_requests=total_requests)
    assert all(s.success for s in samples), "one or more requests failed during the benchmark"

    after_resp = await client.get("/metrics", follow_redirects=True)
    after_resp.raise_for_status()

    delta_text = _delta_histogram_text(before_resp.text, after_resp.text)
    quantiles = histogram_percentiles_from_metrics_text(delta_text, _METRIC_NAME, _LABELS)

    return {
        "concurrency": concurrency,
        "p50_ms": quantiles[0.5] * 1000,
        "p95_ms": quantiles[0.95] * 1000,
        "p99_ms": quantiles[0.99] * 1000,
        "total_requests": total_requests,
    }


@pytest.mark.benchmark
@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", _SWEEP_CONCURRENCIES)
async def test_overhead_sweep(concurrency, benchmark_team):
    client = await gateway_client()
    headers = {"Authorization": f"Bearer {benchmark_team['api_key']}"}
    try:
        metrics = await _measure_overhead_at_concurrency(client, headers, concurrency)
    finally:
        await client.aclose()

    metrics["passed"] = metrics["p95_ms"] < thresholds.OVERHEAD_P95_MS

    result = BenchmarkResult(
        name=f"gateway_overhead_c{concurrency}",
        metrics=metrics,
        thresholds={"p95_ms": thresholds.OVERHEAD_P95_MS},
        passed=metrics["passed"],
    )
    write_result(result)

    _sweep_results.append(metrics)

    # Only the level thresholds.OVERHEAD_P95_MS was actually calibrated
    # against is a hard gate; other levels are reported for visibility only.
    if concurrency == _CALIBRATED_CONCURRENCY:
        assert metrics["p95_ms"] < thresholds.OVERHEAD_P95_MS


@pytest.mark.benchmark
def test_overhead_sweep_summary():
    calibrated = next((level for level in _sweep_results if level["concurrency"] == _CALIBRATED_CONCURRENCY), None)
    assert calibrated is not None, f"concurrency={_CALIBRATED_CONCURRENCY} level missing from sweep results"

    result = BenchmarkResult(
        name="overhead_sweep_summary",
        metrics={"levels": _sweep_results},
        thresholds={"p95_ms_at_concurrency_5": thresholds.OVERHEAD_P95_MS},
        passed=calibrated["passed"],
    )
    write_result(result)
