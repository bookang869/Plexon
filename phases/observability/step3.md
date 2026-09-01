# Step 3: grafana-dashboards

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/PRD.md` — Core Feature 4's dashboard line ("Three Grafana dashboards: Operations (provider health, error rates, fallback events, circuit-breaker status), Business (per-team spend, budget utilization, usage trends), Performance (latency percentiles, token throughput)")
- `/docs/TRD.md` — §8 (the exact metric names these panels must query), §9 (deployment services — `grafana` "preconfigured dashboards provisioned on boot")
- `/docs/ADR.md` — ADR-013 (why Tempo exists — Grafana needs a trace-viewing datasource, not just Prometheus)
- `phases/observability/step1.md`'s actual output: `gateway/observability/metrics.py` — read the real metric/label names as implemented (not this description) so every panel query matches exactly
- `phases/observability/step0.md`'s actual output — confirm the `tempo` service name/port as actually added to `deploy/docker-compose.yml`
- `deploy/docker-compose.yml` — current full service list, to add `grafana` alongside `prometheus`/`tempo`/`postgres` with correct service-name-based URLs (Docker Compose's internal DNS, e.g. `http://prometheus:9090`, not `localhost`)
- `deploy/prometheus/prometheus.yml` — confirm the Prometheus service name/port this step's Grafana datasource must point at

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

This step adds no gateway Python code — it's Grafana provisioning config (datasources + dashboards-as-JSON), auto-loaded on container boot per TRD §9, and the `grafana` Docker Compose service itself.

### 1. Datasource provisioning — `deploy/grafana/provisioning/datasources/datasources.yml`

Two datasources, provisioned so they exist without manual UI setup on first boot:
- Prometheus, pointed at `http://prometheus:9090` (Compose service-name DNS), set as default.
- Tempo, pointed at `http://tempo:3200`.

### 2. Dashboard provisioning — `deploy/grafana/provisioning/dashboards/dashboards.yml`

One "file" provider pointing at a folder (e.g. `/etc/grafana/provisioning/dashboards/json`) where the three dashboard JSON files below live, with a `Plexon` folder name in the Grafana UI.

### 3. Three dashboards — `deploy/grafana/provisioning/dashboards/json/{operations,business,performance}.json`

Each is a real Grafana dashboard JSON (schema version current for a recent Grafana — check `grafana/grafana:latest`'s docs if unsure of the exact `schemaVersion`/panel JSON shape) with actual PromQL panels wired to step 1's metrics — not placeholder/empty panels. Per PRD:

- **Operations** (`operations.json`) — provider health (from `gateway_circuit_breaker_state`, one panel per provider or a table/stat panel across the `provider` label), error rates (`rate(gateway_errors_total[5m])` by provider/error_type), fallback events (`rate(gateway_fallback_triggered_total[5m])` by from/to provider), circuit-breaker status (`gateway_circuit_breaker_state` as a stat/state-timeline panel, and `rate(gateway_circuit_breaker_transitions_total[5m])`).
- **Business** (`business.json`) — per-team spend (`gateway_cost_usd_total` by `team`, as a time series and/or a table of current totals), budget utilization (this metric isn't in TRD §8's Prometheus list — either note the gap in this dashboard's description/text panel rather than inventing a fake query, or add a genuinely simple utilization panel only if you can derive it correctly from existing metrics; don't fabricate a metric name that doesn't exist in `metrics.py`), usage trends (`rate(gateway_requests_total[5m])` by team/model over time).
- **Performance** (`performance.json`) — latency percentiles (`histogram_quantile(0.50/0.95/0.99, rate(gateway_latency_seconds_bucket[5m]))` by provider — note the Prometheus client library's histogram naming convention appends `_bucket`/`_count`/`_sum` to the base name; confirm the exact metric name `metrics.py` actually registers), token throughput (`rate(gateway_tokens_total[5m])` by team/direction).

Every panel's query must reference a metric name/label that actually exists in `gateway/observability/metrics.py` as implemented in step 1 — verify this by reading that file, not by guessing from this description.

### 4. Docker Compose — `deploy/docker-compose.yml`

Add a `grafana` service (image `grafana/grafana:latest`, port `3000`, volumes mounting `./grafana/provisioning:/etc/grafana/provisioning`, `depends_on: [prometheus, tempo]`, and enough env (e.g. `GF_AUTH_ANONYMOUS_ENABLED=true`, `GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer` or `Admin` — your call, this is a portfolio demo, not a production system per the README's Non-Goals) that a fresh `docker compose up` shows working dashboards without a manual login step for the demo recording).

## Acceptance Criteria

```bash
uv run ruff check .   # no Python changes expected in this step; this should be a no-op pass
python3 -c "import json; [json.load(open(f'deploy/grafana/provisioning/dashboards/json/{n}.json')) for n in ('operations','business','performance')]"
python3 -c "import yaml; yaml.safe_load(open('deploy/grafana/provisioning/datasources/datasources.yml')); yaml.safe_load(open('deploy/grafana/provisioning/dashboards/dashboards.yml'))"
docker compose -f deploy/docker-compose.yml config -q   # validates the compose file parses/references are consistent
```

Since this step is infrastructure config with no pytest-covered logic, the AC is deliberately: valid JSON/YAML, and `docker compose config` succeeding (catches typos in service references/volume paths). If you have the ability to actually bring up the full stack (`docker compose -f deploy/docker-compose.yml up -d`) and hit `http://localhost:3000` to confirm the three dashboards load with the Prometheus/Tempo datasources attached and panels render without "no data source" errors, do that as an extra manual check — but don't block completion on live traffic existing to populate the panels with non-empty data (metrics existing at all, from an idle gateway process, is enough to prove the wiring works).

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does every panel query reference a metric name that actually exists in `gateway/observability/metrics.py`?
   - Do the Prometheus/Tempo datasource URLs use Compose service-name DNS (`http://prometheus:9090`, `http://tempo:3200`), not `localhost`?
   - Does the `grafana` service's `depends_on` include both `prometheus` and `tempo`?
3. Based on the result, update `phases/observability/index.json` step 3:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files created, which panels reference which metrics, any noted gap (e.g. budget-utilization metric absence) -- marking the observability phase complete"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't invent a Prometheus metric name that doesn't exist in `gateway/observability/metrics.py` just to fill a dashboard panel (e.g. a fabricated `gateway_budget_utilization` gauge). Reason: TRD §8's metric list is the literal spec from step 1 — a panel querying a nonexistent metric silently shows "no data" forever and is worse than an honestly-labeled gap.
- Don't add real Grafana Alerting rules to these dashboards. Reason: step 2 already decided alert evaluation is gateway-owned Redis-backed logic, not Grafana-side rules — duplicating alert logic here would create two disagreeing sources of truth for the same conditions.
- Do not break existing tests.
