"""Rate-limit admission accuracy and budget overshoot quantification
benchmark (step 4 of phases/benchmarks; TRD SS10 "Rate-limit accuracy |
correct under concurrent load (no over/under-admission)", and the
check-then-act budget race documented in gateway/ratelimit/budget.py /
accepted under ADR-002).

Deliberately similar in shape to tests/integration/test_concurrent_
ratelimit_budget.py (same `_insert_team`/`_delete_team`-style helpers, same
isolated-dimension technique for rpm), but different in purpose: that file
makes an *exact* admitted-count assertion at a small, fixed concurrency
(10-30 requests) as a correctness gate -- not duplicated here. This file
instead measures whether that same exact-admission guarantee still holds at
a higher concurrency (rpm dimension, still asserted exactly since
`check_and_consume` is a single atomic Redis EVAL -- over/under-admission at
any concurrency is a real bug, not a tolerance-worthy race), and
*quantifies* -- rather than only asserts eventual rejection for -- the
budget check-then-act overshoot at a higher concurrency than that file's
5-request wave.

Each test uses its own dedicated, tightly-sized team (not the shared
`benchmark_team` fixture from step 0) so the rpm/tpm/budget crossing point
stays observable within a practical request count -- `benchmark_team`'s
generous limits, sized for throughput/overhead/failover benchmarks, would
require an impractically large N here.

The rpm admission-accuracy test runs its 200-concurrent wave across
`_RPM_TRIALS` independent trials (each against a fresh team, since the
token bucket's window state is per-team in Redis) to accumulate a larger
aggregate sample (~10,000 requests by default) than a single wave can
provide, reporting "0 incorrect admissions across N requests" rather than
"correct once".
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

import httpx
import pytest

from benchmarks import thresholds
from benchmarks.common import (
    GATEWAY_BASE_URL,
    BenchmarkResult,
    gateway_client,
    percentiles,
    run_concurrent,
    write_result,
)

_MODEL = "gpt-4o-mini"


async def _insert_team(
    db_pool,
    *,
    rpm_limit: int = 1_000_000,
    tpm_limit: int = 1_000_000,
    daily_budget_usd: str | None = None,
) -> dict:
    team_id = f"team-bench-rl-{uuid.uuid4().hex[:8]}"
    api_key = f"bench-rl-key-{uuid.uuid4().hex}"
    await db_pool.execute(
        """
        INSERT INTO teams (id, name, allowed_models, rpm_limit, tpm_limit,
                            daily_budget_usd, monthly_budget_usd, config)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        team_id,
        "Ratelimit Budget Benchmark Team",
        [_MODEL],
        rpm_limit,
        tpm_limit,
        daily_budget_usd,
        None,
        "{}",
    )
    await db_pool.execute(
        "INSERT INTO team_api_keys (token, team_id) VALUES ($1, $2)", api_key, team_id
    )
    return {"team_id": team_id, "api_key": api_key}


async def _delete_team(db_pool, team_id: str) -> None:
    await db_pool.execute("DELETE FROM spend_ledger WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM alert_history WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM team_api_keys WHERE team_id = $1", team_id)
    await db_pool.execute("DELETE FROM teams WHERE id = $1", team_id)


def _payload() -> dict:
    return {"model": _MODEL, "messages": [{"role": "user", "content": "hi there"}]}


@dataclass
class _StatusSample:
    status_code: int
    elapsed_seconds: float


async def _make_status_request(client: httpx.AsyncClient, headers: dict) -> _StatusSample:
    started = time.monotonic()
    resp = await client.post("/v1/chat/completions", json=_payload(), headers=headers)
    return _StatusSample(status_code=resp.status_code, elapsed_seconds=time.monotonic() - started)


# --- rpm admission accuracy at scale -----------------------------------------

_RPM_LIMIT = 30
_RPM_TOTAL_REQUESTS = 200
# 50 trials x 200 requests/trial = ~10,000 total requests, a large enough
# aggregate sample to report "0 incorrect admissions across N requests"
# rather than "correct once". Each trial uses a fresh team (see module
# docstring) so the sample stays N independent from-cold admission tests.
_RPM_TRIALS = int(os.environ.get("PLEXON_BENCHMARK_RATELIMIT_TRIALS", "50"))


async def _run_rpm_trial(db_pool, trial: int) -> dict:
    team = await _insert_team(db_pool, rpm_limit=_RPM_LIMIT, tpm_limit=100_000_000)

    # A dedicated client with a raised connection-pool ceiling: the default
    # (httpx's own limit of 100) would otherwise force part of this burst to
    # queue for a socket before ever reaching the gateway, polluting the
    # rejection-latency measurement with client-side queuing rather than
    # gateway/DB-side behavior.
    client = httpx.AsyncClient(
        base_url=GATEWAY_BASE_URL,
        timeout=30.0,
        limits=httpx.Limits(max_connections=_RPM_TOTAL_REQUESTS + 20),
    )
    try:
        headers = {"Authorization": f"Bearer {team['api_key']}"}

        async def _make_request() -> _StatusSample:
            return await _make_status_request(client, headers)

        samples = await run_concurrent(
            _make_request, concurrency=_RPM_TOTAL_REQUESTS, total_requests=_RPM_TOTAL_REQUESTS
        )
    finally:
        await client.aclose()
        await _delete_team(db_pool, team["team_id"])

    admitted = [s for s in samples if s.status_code == 200]
    rejected = [s for s in samples if s.status_code == 429]
    assert len(admitted) + len(rejected) == len(samples), "unexpected non-200/429 status in wave"

    return {
        "trial": trial,
        "admitted_count": len(admitted),
        "rejected_count": len(rejected),
        "rejected_latencies_seconds": [s.elapsed_seconds for s in rejected],
    }


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_rpm_admission_accuracy_at_higher_concurrency(db_pool):
    trial_results = [await _run_rpm_trial(db_pool, trial) for trial in range(_RPM_TRIALS)]

    total_requests = sum(t["admitted_count"] + t["rejected_count"] for t in trial_results)
    incorrect_admission_count = sum(
        abs(t["admitted_count"] - _RPM_LIMIT) for t in trial_results
    )
    pooled_rejected_latencies = [
        latency for t in trial_results for latency in t["rejected_latencies_seconds"]
    ]
    rejection_quantiles = percentiles(pooled_rejected_latencies)
    rejection_p95_ms = rejection_quantiles[0.95] * 1000

    per_trial = [
        {
            "trial": t["trial"],
            "admitted_count": t["admitted_count"],
            "rejected_count": t["rejected_count"],
        }
        for t in trial_results
    ]

    result = BenchmarkResult(
        name="ratelimit_admission_accuracy",
        metrics={
            "trials": _RPM_TRIALS,
            "total_requests": total_requests,
            "incorrect_admission_count": incorrect_admission_count,
            "rejection_p50_ms": rejection_quantiles[0.5] * 1000,
            "rejection_p95_ms": rejection_p95_ms,
            "rejection_p99_ms": rejection_quantiles[0.99] * 1000,
            "per_trial": per_trial,
        },
        thresholds={
            "expected_incorrect_admissions": 0,
            "rejection_p95_max_ms": thresholds.RATELIMIT_REJECTION_P95_MAX_MS,
        },
        passed=(
            incorrect_admission_count == 0
            and rejection_p95_ms < thresholds.RATELIMIT_REJECTION_P95_MAX_MS
        ),
    )
    write_result(result)

    # Exact, not a tolerance range: check_and_consume is a single atomic
    # Redis EVAL, so over/under-admission at any concurrency is a genuine bug.
    assert incorrect_admission_count == 0
    assert rejection_p95_ms < thresholds.RATELIMIT_REJECTION_P95_MAX_MS


# --- budget overshoot quantification -----------------------------------------

_BUDGET_WAVE_CONCURRENCY = 40
# gpt-4o-mini pricing (config.yaml) applied to the mock's fixed fabricated
# usage for the "hi there" payload -- prompt_tokens=2 ("hi there" word
# count), completion_tokens=6 ("Mock OpenAI reply to: hi there" word count,
# mocks/mock_openai/app.py) -> (2/1000)*0.00015 + (6/1000)*0.0006.
_COST_PER_REQUEST = Decimal("0.0000039")
# Cap is set to this fraction below the full wave's total cost, so that even
# in the worst case -- every one of _BUDGET_WAVE_CONCURRENCY requests slips
# past the check-then-act race and gets admitted -- overshoot_pct is bounded
# by _CAP_SAFETY_MARGIN / (1 - _CAP_SAFETY_MARGIN), well under
# thresholds.BUDGET_OVERSHOOT_MAX_PCT, regardless of how the real race
# resolves (admitted_count can never exceed the wave size).
_CAP_SAFETY_MARGIN = Decimal("0.03")
_SECOND_WAVE_SIZE = 5


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_budget_overshoot_under_concurrency(db_pool):
    cap = _COST_PER_REQUEST * _BUDGET_WAVE_CONCURRENCY * (Decimal(1) - _CAP_SAFETY_MARGIN)
    team = await _insert_team(
        db_pool, rpm_limit=1_000_000, tpm_limit=1_000_000, daily_budget_usd=str(cap)
    )
    client = await gateway_client()
    try:
        headers = {"Authorization": f"Bearer {team['api_key']}"}

        async def _make_request() -> _StatusSample:
            return await _make_status_request(client, headers)

        # First wave: some may succeed, some may 402 depending on how the
        # check-then-act race resolves -- not asserted exactly, that's the
        # point (see module docstring / budget.py's docstring).
        first_wave = await run_concurrent(
            _make_request,
            concurrency=_BUDGET_WAVE_CONCURRENCY,
            total_requests=_BUDGET_WAVE_CONCURRENCY,
        )
        assert all(s.status_code in (200, 402) for s in first_wave)

        # record_spend is awaited before the response returns
        # (gateway/routes.py), so by the time run_concurrent's gather above
        # has returned, the first wave has already fully settled -- no extra
        # sleep needed before reading spend_ledger.
        settled_spend = await db_pool.fetchval(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM spend_ledger WHERE team_id = $1",
            team["team_id"],
        )
        cap_usd = float(cap)
        settled_spend_usd = float(settled_spend)
        overshoot_usd = max(0.0, settled_spend_usd - cap_usd)
        overshoot_pct = (overshoot_usd / cap_usd) * 100.0 if cap_usd > 0 else 0.0

        # Second wave, fired only after the first has fully settled --
        # tracked spend is now at/over cap_usd, so every request in this
        # wave must be uniformly rejected.
        second_wave = await run_concurrent(
            _make_request, concurrency=_SECOND_WAVE_SIZE, total_requests=_SECOND_WAVE_SIZE
        )
    finally:
        await client.aclose()
        await _delete_team(db_pool, team["team_id"])

    result = BenchmarkResult(
        name="budget_overshoot",
        metrics={
            "concurrency": _BUDGET_WAVE_CONCURRENCY,
            "cap_usd": cap_usd,
            "settled_spend_usd": settled_spend_usd,
            "overshoot_usd": overshoot_usd,
            "overshoot_pct": overshoot_pct,
        },
        thresholds={"overshoot_max_pct": thresholds.BUDGET_OVERSHOOT_MAX_PCT},
        passed=overshoot_pct < thresholds.BUDGET_OVERSHOOT_MAX_PCT,
        notes=(
            f"overshoot_pct is relative to this run's concurrency level "
            f"({_BUDGET_WAVE_CONCURRENCY}) -- a regression baseline at this "
            "fixed concurrency, not a fixed spec value"
        ),
    )
    write_result(result)

    assert overshoot_pct < thresholds.BUDGET_OVERSHOOT_MAX_PCT
    assert [s.status_code for s in second_wave] == [402] * _SECOND_WAVE_SIZE
