"""Gateway-overhead benchmark (step 1 of phases/benchmarks; TRD §8/§10,
ADR-026). Validates `gateway_overhead_seconds` as reported by the live
`/metrics` endpoint -- requires the full docker-compose stack including the
gateway container itself (see benchmarks/common.py's gateway_client()); an
in-process ASGITransport client wouldn't be observing the same process the
real running gateway serves metrics from.
"""

from __future__ import annotations

import time

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
_CONCURRENCY = 5

_METRIC_NAME = "gateway_overhead_seconds"
_LABELS = {"route": "chat_completion"}


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


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_gateway_overhead_percentiles(benchmark_team):
    client = await gateway_client()
    headers = {"Authorization": f"Bearer {benchmark_team['api_key']}"}
    try:
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

        samples = await run_concurrent(
            _make_request, concurrency=_CONCURRENCY, total_requests=_TOTAL_REQUESTS
        )
        assert all(s.success for s in samples), "one or more requests failed during the benchmark"

        after_resp = await client.get("/metrics", follow_redirects=True)
        after_resp.raise_for_status()
    finally:
        await client.aclose()

    delta_text = _delta_histogram_text(before_resp.text, after_resp.text)
    quantiles = histogram_percentiles_from_metrics_text(delta_text, _METRIC_NAME, _LABELS)
    p50_ms = quantiles[0.5] * 1000
    p95_ms = quantiles[0.95] * 1000
    p99_ms = quantiles[0.99] * 1000

    result = BenchmarkResult(
        name="gateway_overhead",
        metrics={"p50_ms": p50_ms, "p95_ms": p95_ms, "p99_ms": p99_ms},
        thresholds={"p95_ms": thresholds.OVERHEAD_P95_MS},
        passed=p95_ms < thresholds.OVERHEAD_P95_MS,
    )
    write_result(result)

    assert p95_ms < thresholds.OVERHEAD_P95_MS
