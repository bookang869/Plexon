"""Shared helpers for the manually-invoked performance benchmark suite
(phases/benchmarks). Unlike tests/, these measure the live docker-compose
stack over real network I/O (like tests/load/locustfile.py) rather than the
in-process app -- see gateway_client() below.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from prometheus_client.parser import text_string_to_metric_families

GATEWAY_BASE_URL = os.environ.get("PLEXON_BENCHMARK_BASE_URL", "http://localhost:8000")


async def gateway_client() -> httpx.AsyncClient:
    """Plain httpx.AsyncClient (real network, no ASGITransport) pointed at
    GATEWAY_BASE_URL -- benchmarks measure the live docker-compose stack,
    not the in-process app the tests/ suite uses, since real network/
    serialization overhead is part of what's being measured.
    """
    return httpx.AsyncClient(base_url=GATEWAY_BASE_URL, timeout=30.0)


@dataclass
class LatencySample:
    started_at: float
    elapsed_seconds: float
    success: bool


async def run_concurrent(
    make_request: Callable[[], Awaitable[LatencySample]],
    *,
    concurrency: int,
    total_requests: int | None = None,
    duration_seconds: float | None = None,
) -> list[LatencySample]:
    """Runs `concurrency` async workers looping on make_request() until
    either total_requests samples have been collected (fixed-count mode) or
    duration_seconds has elapsed (sustained-load mode) -- exactly one of the
    two must be provided.
    """
    if (total_requests is None) == (duration_seconds is None):
        raise ValueError("exactly one of total_requests or duration_seconds must be provided")

    per_worker_results: list[list[LatencySample]] = [[] for _ in range(concurrency)]

    if total_requests is not None:
        remaining = total_requests
        lock = asyncio.Lock()

        async def worker(idx: int) -> None:
            nonlocal remaining
            while True:
                async with lock:
                    if remaining <= 0:
                        return
                    remaining -= 1
                per_worker_results[idx].append(await make_request())

        await asyncio.gather(*(worker(i) for i in range(concurrency)))
    else:
        deadline = time.monotonic() + duration_seconds

        async def worker(idx: int) -> None:
            while time.monotonic() < deadline:
                per_worker_results[idx].append(await make_request())

        await asyncio.gather(*(worker(i) for i in range(concurrency)))

    return [sample for worker_samples in per_worker_results for sample in worker_samples]


def percentiles(samples: list[float], points: tuple[float, ...] = (0.5, 0.95, 0.99)) -> dict[float, float]:
    """Pure client-side percentile (sorted-list index method) over a list of
    raw floats -- used by benchmarks that time requests themselves
    client-side (throughput, failover latency).
    """
    if not samples:
        return {p: 0.0 for p in points}
    ordered = sorted(samples)
    n = len(ordered)
    return {p: ordered[min(int(p * n), n - 1)] for p in points}


def histogram_percentiles_from_metrics_text(
    metrics_text: str,
    metric_name: str,
    label_filter: dict[str, str] | None,
    points: tuple[float, ...] = (0.5, 0.95, 0.99),
) -> dict[float, float]:
    """Parses Prometheus text-exposition output for the named Histogram,
    filters bucket samples by label_filter if given, and estimates each
    requested quantile by linear interpolation within the bucket whose
    cumulative count first reaches quantile * total_count (the standard
    Prometheus histogram_quantile approximation).
    """
    label_filter = label_filter or {}
    bucket_counts: dict[float, float] = {}

    for family in text_string_to_metric_families(metrics_text):
        if family.name != metric_name:
            continue
        for sample in family.samples:
            if not sample.name.endswith("_bucket"):
                continue
            if any(sample.labels.get(k) != v for k, v in label_filter.items()):
                continue
            le = float(sample.labels["le"])
            bucket_counts[le] = bucket_counts.get(le, 0.0) + sample.value

    if not bucket_counts:
        return {p: 0.0 for p in points}

    sorted_bounds = sorted(bucket_counts)
    total_count = bucket_counts[sorted_bounds[-1]]

    result: dict[float, float] = {}
    for p in points:
        target = p * total_count
        prev_bound, prev_count = 0.0, 0.0
        estimate = sorted_bounds[-1]
        for bound in sorted_bounds:
            count = bucket_counts[bound]
            if count >= target:
                if bound == float("inf"):
                    estimate = prev_bound
                elif count == prev_count:
                    estimate = bound
                else:
                    fraction = (target - prev_count) / (count - prev_count)
                    estimate = prev_bound + fraction * (bound - prev_bound)
                break
            prev_bound, prev_count = bound, count
        result[p] = estimate

    return result


@dataclass
class BenchmarkResult:
    name: str
    metrics: dict[str, float]
    thresholds: dict[str, float]
    passed: bool
    notes: str = ""


def write_result(result: BenchmarkResult, results_dir: Path = Path("benchmarks/results")) -> None:
    """Writes {results_dir}/{result.name}.json and {result.name}.csv, and
    prints a human-readable summary to stdout. Call this BEFORE any
    threshold assertion in the calling test, so a failing assertion still
    leaves the measured numbers on disk instead of only a traceback.
    """
    results_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "name": result.name,
        "generated_at": datetime.now(UTC).isoformat(),
        "metrics": result.metrics,
        "thresholds": result.thresholds,
        "passed": result.passed,
        "notes": result.notes,
    }
    (results_dir / f"{result.name}.json").write_text(json.dumps(payload, indent=2))

    with (results_dir / f"{result.name}.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        for key, value in result.metrics.items():
            writer.writerow([key, value])
        for key, value in result.thresholds.items():
            writer.writerow([f"threshold:{key}", value])
        writer.writerow(["passed", result.passed])

    print_summary(result)


def print_summary(result: BenchmarkResult) -> None:
    status = "PASS" if result.passed else "FAIL"
    print(f"\n=== benchmark: {result.name} [{status}] ===")
    for key, value in result.metrics.items():
        threshold = result.thresholds.get(key)
        suffix = f" (threshold: {threshold})" if threshold is not None else ""
        print(f"  {key}: {value}{suffix}")
    if result.notes:
        print(f"  notes: {result.notes}")
    print()
