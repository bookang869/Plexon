# Step 0: benchmark-harness

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 5 (load-test/NFR language: "Target: <10ms gateway overhead latency. Verify rate-limit accuracy, fallback under simulated outage...")
- `/docs/TRD.md` — §10 (Non-Functional Requirements table: gateway overhead <10ms, 5,000+ concurrent, rate-limit accuracy, circuit breaker), §11 (project structure)
- `/docs/ADR.md` — ADR-026 (`prometheus-client` direct scrape, not an OTel metrics exporter — this is what makes `/metrics` scrapeable without a running Prometheus server), ADR-002 (portfolio/demo rigor — informs how strict thresholds should be)
- `gateway/observability/metrics.py` — every existing metric name/label set; this is the module the benchmark suite reads from via `/metrics`
- `gateway/main.py` — `app.mount("/metrics", make_asgi_app())` — confirms `/metrics` is a direct Prometheus text-exposition endpoint on the gateway itself, no separate Prometheus server needed to read it
- `tests/load/locustfile.py` — the existing large-scale load test this new suite is *not* replacing; read its module docstring in full, especially the "Gateway-overhead latency" paragraph explaining why it can only approximate overhead today (this new suite's step 1 fixes that gap directly in the gateway)
- `tests/conftest.py` — `db_pool`/`redis_client` fixture pattern (`init_pool`/`get_pool`/`close_pool`, `init_redis`/`get_redis`/`close_redis`) — this step's `benchmarks/conftest.py` mirrors it, pointed at the live docker-compose stack instead of an in-process app
- `tests/integration/test_concurrent_ratelimit_budget.py` and `tests/integration/test_concurrent_resilience.py` — `_insert_team`/`_delete_team` helper pattern — this step's `benchmark_team` fixture follows the same shape
- `deploy/docker-compose.yml` — full service list and published host ports (`gateway:8000`, `postgres:5433`, `redis:6379`, `mock-openai:8081`, `mock-anthropic:8082`) — the benchmark suite runs on the host against these published ports, the same posture as Locust
- `pyproject.toml` — current `[tool.pytest.ini_options]` (there is none yet) and `[dependency-groups].dev`
- `.gitignore` — current entries (`scripts/demo_teams.json` is the precedent for gitignoring generated-at-runtime files)

Read carefully through the code produced in previous phases, understand the design intent, and then start working.

## Task

This step only builds shared infrastructure — no benchmark yet asserts anything about gateway performance. Every later step (1-4) imports from `benchmarks/common.py` and uses the fixtures in `benchmarks/conftest.py`.

### 1. `benchmarks/__init__.py`

Empty, makes `benchmarks` an importable package (matches `tests/__init__.py`).

### 2. `benchmarks/common.py`

```python
GATEWAY_BASE_URL = ...  # os.environ.get("PLEXON_BENCHMARK_BASE_URL", "http://localhost:8000")

async def gateway_client() -> httpx.AsyncClient:
    """Plain httpx.AsyncClient (real network, no ASGITransport) pointed at
    GATEWAY_BASE_URL -- benchmarks measure the live docker-compose stack
    (like tests/load/locustfile.py), not the in-process app the tests/
    suite uses, since real network/serialization overhead is part of what's
    being measured."""

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
    two must be provided. Each worker appends its own LatencySample list;
    results are concatenated and returned in completion order. Uses
    asyncio.Semaphore or a worker-pool pattern, not asyncio.gather over a
    pre-built list of total_requests coroutines, so sustained-duration mode
    doesn't need to know the count up front."""

def percentiles(samples: list[float], points: tuple[float, ...] = (0.5, 0.95, 0.99)) -> dict[float, float]:
    """Pure client-side percentile (sorted-list index method) over a list of
    raw floats -- used by benchmarks that time requests themselves
    client-side (throughput, failover latency)."""

def histogram_percentiles_from_metrics_text(
    metrics_text: str, metric_name: str, label_filter: dict[str, str] | None, points: tuple[float, ...] = (0.5, 0.95, 0.99)
) -> dict[float, float]:
    """Parses Prometheus text-exposition output (via
    prometheus_client.parser.text_string_to_metric_families) for the named
    Histogram, filters bucket samples by label_filter if given, and
    estimates each requested quantile by linear interpolation within the
    bucket whose cumulative count first reaches quantile * total_count
    (the standard Prometheus histogram_quantile approximation) -- used by
    the gateway-overhead benchmark (step 1) to read gateway_overhead_seconds
    off the live /metrics endpoint instead of timing client-side, since the
    metric being measured is specifically gateway-only time that isn't
    directly observable from outside."""

@dataclass
class BenchmarkResult:
    name: str
    metrics: dict[str, float]
    thresholds: dict[str, float]
    passed: bool
    notes: str = ""

def write_result(result: BenchmarkResult, results_dir: Path = Path("benchmarks/results")) -> None:
    """Writes {results_dir}/{result.name}.json and {result.name}.csv
    (creating results_dir if needed), and prints a human-readable summary
    table to stdout via print_summary. Call this BEFORE any threshold
    assertion in the calling test, so a failing assertion still leaves the
    measured numbers on disk and in the console instead of only a
    traceback."""

def print_summary(result: BenchmarkResult) -> None: ...
```

Timestamps in `write_result`'s output may use `datetime.now(timezone.utc).isoformat()` freely — no restriction applies here, this is regular application code, not a workflow script.

### 3. `benchmarks/thresholds.py`

One named constant per assertion used in steps 1-4 (e.g. `OVERHEAD_P95_MS`, `THROUGHPUT_MIN_RPS`, `FAILOVER_RELIABILITY_MIN_PCT`, `FAILOVER_SWITCH_MAX_SECONDS`, `RECOVERY_MAX_SECONDS_OVER_COOLDOWN`, `RATELIMIT_ADMIT_TOLERANCE`, `BUDGET_OVERSHOOT_MAX_PCT`) — a one-line comment above each explaining what it's calibrated against (e.g. TRD §10's target vs. what's realistically achievable in a single-process/Dockerized-dev-machine benchmark). Leave the exact numeric values to your judgment; they only need to exist as named constants for later steps to import — don't invent the later steps' logic here.

### 4. `benchmarks/conftest.py`

`db_pool` fixture (session- or function-scoped, your call) using `gateway.db.init_pool`/`get_pool`/`close_pool` against `PLEXON_DATABASE_URL` (default it to `postgresql://plexon:plexon@localhost:5433/plexon` the same way `tests/conftest.py` does, via `os.environ.setdefault`). A `benchmark_team` fixture that inserts a team with generous rpm/tpm limits and a large budget (so throughput/overhead/failover benchmarks aren't incidentally rate-limited or budget-blocked) and tears it down after, same `_insert_team`/`_delete_team` shape as the integration tests.

### 5. `pyproject.toml`

Add:
```toml
[tool.pytest.ini_options]
markers = ["benchmark: performance benchmark, excluded from default `pytest` runs -- invoke via `pytest -m benchmark` or `pytest benchmarks/`"]
addopts = "-m 'not benchmark'"
```
This must not change what plain `pytest` / `pytest -v` (CI's own invocation) collects or runs today — verify by re-running the existing full suite after this edit.

### 6. `.gitignore`

Add `benchmarks/results/` — generated output, same reasoning as the existing `scripts/demo_teams.json` entry.

### 7. `benchmarks/test_common.py`

A plain (non-`benchmark`-marked) unit test of `percentiles()` and `histogram_percentiles_from_metrics_text()` against small hand-constructed inputs (a known sorted list for the former; a small hand-written Prometheus text block with a `_bucket`/`_sum`/`_count` for a fake histogram for the latter) — this is the only thing this step can verify automatically, since nothing here yet talks to a live gateway.

## Acceptance Criteria

```bash
uv run ruff check .
uv run pytest -v                        # existing full suite: must still pass, unaffected by the new addopts
uv run pytest benchmarks/test_common.py -v   # new unit tests pass
uv run pytest benchmarks/ --collect-only -q  # confirms nothing here is silently swept into a default `pytest` run
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does `addopts = "-m 'not benchmark'"` leave the existing test suite's pass/fail count in `uv run pytest -v` identical to before this step?
   - Does `benchmarks/conftest.py` reuse `gateway.db`'s pool functions rather than opening an ad hoc `asyncpg` connection (same rule step 0 of `test-load` enforced for `scripts/setup_demo_teams.py`)?
   - Is every constant in `thresholds.py` named and commented with what it's calibrated against, not a bare unexplained number?
3. Based on the result, update `phases/benchmarks/index.json` step 0:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created, the common.py helper signatures actually implemented, the pytest marker/addopts wiring, thresholds.py's constant names"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't add `benchmarks/` to any CI-blocking workflow step. Reason: this suite times real wall-clock behavior against a live Docker stack (including a later step's real 30s circuit-breaker cooldown wait) — it's a manually-invoked local signal, not a merge gate; wiring it into `.github/workflows/ci.yml` is out of scope for this step and this phase.
- Don't use `numpy` or any other new runtime dependency for percentile math. Reason: a sorted-list index calculation over at most a few thousand floats needs no numerical library; keep `pyproject.toml`'s dependency surface as small as the rest of this codebase has kept it.
- Don't make `addopts` exclude anything beyond the new `benchmark` marker (e.g. don't accidentally scope it to only run `tests/`). Reason: the existing `tests/` suite (including `tests/integration/`) must keep running exactly as it does today.
- Do not break existing tests.
