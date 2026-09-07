# Step 4: ratelimit-budget-benchmark

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/TRD.md` — §10 ("Rate-limit accuracy | correct under concurrent load (no over/under-admission)")
- `/docs/ADR.md` — ADR-002 (portfolio/demo rigor — the rationale `gateway/ratelimit/limiter.py` cites for accepting a small rpm/tpm check-then-act race window; this step measures/quantifies that window rather than treating it as untestable)
- `phases/benchmarks/step0.md`'s actual output — `benchmarks/common.py`, `benchmarks/conftest.py`'s `db_pool`, `benchmarks/thresholds.py`'s rate-limit/budget constants
- `gateway/ratelimit/limiter.py` — `check_rate_limit`'s docstring explicitly describing the accepted rpm→tpm race window (ADR-002) — this step's rpm/tpm accuracy test should isolate one dimension at a time exactly like the existing integration test does, just at higher concurrency
- `gateway/ratelimit/budget.py` — `check_budget`/`record_spend` — read the check-then-act split this step's overshoot measurement quantifies
- `tests/integration/test_concurrent_ratelimit_budget.py` — read this file in full. This step's benchmark is deliberately similar in shape (same `_insert_team`-style helper, same isolated-dimension technique) but different in purpose: that file makes an *exact* admitted-count assertion at a small, fixed concurrency (10-30 requests) as a correctness gate; this step measures accuracy and *quantifies* overshoot at a higher concurrency as a performance/regression signal, not a strict pass/fail correctness re-test (that correctness coverage already exists and isn't being duplicated here)
- `deploy/schema.sql` — `spend_ledger` columns, used to read settled spend directly

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

Create `benchmarks/bench_ratelimit_budget.py`. Local helper(s) for inserting/deleting a dedicated test team per test (same shape as `test_concurrent_ratelimit_budget.py`'s `_insert_team`/`_delete_team`, reusing `benchmarks/conftest.py`'s `db_pool`) — don't reuse the shared `benchmark_team` fixture for these tests, since each one needs its own tightly-sized rpm/tpm/budget values to make the measurement meaningful, the same reason the existing integration test doesn't reuse `seeded_team` either.

### 1. Admission accuracy at scale

```python
@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_rpm_admission_accuracy_at_higher_concurrency(db_pool):
    """Same isolated-rpm-dimension technique as test_concurrent_ratelimit_
    budget.py's test_concurrent_requests_admit_exactly_rpm_limit (tpm
    capacity set effectively unlimited), but at a higher N (e.g. a few
    hundred concurrent requests against a two-figure rpm_limit) than that
    file's 30-request check -- measures whether exact-admission still holds
    (admitted_count == rpm_limit, no over/under-admission) as concurrency
    scales up, plus the p95 latency of the rejected (429) responses
    (rejections should be fast, not queued behind the accepted requests).
    write_result() with admitted_count, expected rpm_limit, and rejection-
    latency percentiles, then asserts admitted_count == rpm_limit exactly
    (over/under-admission at any concurrency is a real bug, not a
    tolerance-worthy race) and rejection p95 latency is small (see
    thresholds.py)."""
```

### 2. Budget overshoot quantification

```python
@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_budget_overshoot_under_concurrency(db_pool):
    """Same setup as test_concurrent_ratelimit_budget.py's budget test
    (small daily_budget_usd sized so a handful of requests crosses it), but
    reports the actual overshoot rather than only asserting eventual
    rejection: fires one concurrent wave sized larger than that file's
    (e.g. a few dozen), waits for it to settle, reads settled spend from
    spend_ledger, computes overshoot_usd = max(0, settled_spend - cap) and
    overshoot_pct = overshoot_usd / cap. write_result() with these numbers
    plus the concurrency level used (this number is inherently a function
    of concurrency -- more in-flight requests at the moment the cap is
    crossed means more can slip through the check-then-act race, per ADR-002
    -- so treat this run's result as a regression baseline at this fixed
    concurrency, not a spec value). Then asserts overshoot_pct <
    thresholds.BUDGET_OVERSHOOT_MAX_PCT, and separately confirms (like the
    existing integration test) that a second wave fired after settling is
    uniformly rejected."""
```

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build
uv run python3 scripts/setup_demo_teams.py
uv run pytest benchmarks/bench_ratelimit_budget.py -m benchmark -v
docker compose -f deploy/docker-compose.yml down
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does the rpm accuracy test assert an *exact* admitted count (no tolerance), consistent with `check_and_consume`'s single-atomic-Lua-script guarantee (`gateway/ratelimit/token_bucket.py`)?
   - Does the budget overshoot test record the concurrency level alongside the measured overshoot, making clear the number is concurrency-relative rather than an absolute spec value?
   - Does this file avoid duplicating `test_concurrent_ratelimit_budget.py`'s existing exact-count correctness assertions as its primary claim (its job is measurement/quantification at scale, not re-proving base correctness)?
3. Based on the result, update `phases/benchmarks/index.json` step 4:
   - Success → `"status": "completed"`, `"summary": "one-line summary — file created, concurrency levels used, measured admission accuracy and budget overshoot numbers, and whether each passed its threshold"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't relax the rpm/tpm admission-count assertion to a tolerance range. Reason: `check_and_consume` is a single atomic Redis `EVAL` (documented in `tests/integration/test_concurrent_ratelimit_budget.py`'s own module docstring) — over/under-admission at any concurrency is a genuine bug, not an acceptable race, unlike the budget check which is a documented (ADR-002) check-then-act race.
- Don't present the measured budget overshoot as a fixed acceptable spec number rather than a concurrency-relative regression baseline. Reason: the size of the check-then-act race window scales with how many requests are in flight when the cap is crossed — a number reported without its concurrency level is meaningless to compare against on a future run.
- Don't reuse `benchmark_team` (step 0) for these tests. Reason: both tests need small, deliberately-sized rpm/tpm/budget values to make the crossing point observable within a reasonable request count — `benchmark_team`'s generous limits (sized for throughput/overhead benchmarks) would require an impractically large N here.
- Do not break existing tests.
