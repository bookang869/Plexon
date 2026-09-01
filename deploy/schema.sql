-- Team registry & config (admin-editable, no restart required)
CREATE TABLE teams (
  id              text PRIMARY KEY,        -- e.g. "team-acme"
  name            text NOT NULL,
  allowed_models  text[] NOT NULL,
  rpm_limit       int NOT NULL,
  tpm_limit       int NOT NULL,
  daily_budget_usd  numeric,
  monthly_budget_usd numeric,
  config          jsonb NOT NULL DEFAULT '{}',  -- per-team enrichment/content-filter overrides (ADR-021)
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Team API keys (opaque token -> team), separate from admin tokens (ADR-012)
CREATE TABLE team_api_keys (
  token       text PRIMARY KEY,
  team_id     text NOT NULL REFERENCES teams(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz
);

-- Named admin tokens (opaque token -> admin identity), ADR-012
CREATE TABLE admin_tokens (
  token       text PRIMARY KEY,
  admin_name  text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz
);

-- Per-request spend ledger, source of truth for budget/spend (ADR-004)
CREATE TABLE spend_ledger (
  id              bigserial PRIMARY KEY,
  team_id         text NOT NULL REFERENCES teams(id),
  provider        text NOT NULL,
  model           text NOT NULL,
  input_tokens    int NOT NULL,
  output_tokens   int NOT NULL,
  cost_usd        numeric NOT NULL,
  request_id      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- Admin API audit log (who changed what, when) (ADR-012)
CREATE TABLE audit_log (
  id          bigserial PRIMARY KEY,
  admin_name  text NOT NULL,
  action      text NOT NULL,          -- e.g. "update_rate_limit"
  team_id     text REFERENCES teams(id),
  before      jsonb,
  after       jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Circuit-breaker state-change history (ADR-010)
CREATE TABLE circuit_breaker_history (
  id          bigserial PRIMARY KEY,
  provider    text NOT NULL,
  from_state  text NOT NULL,          -- closed | open | half_open
  to_state    text NOT NULL,
  reason      text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Provider health history, for post-incident analysis
CREATE TABLE provider_health_history (
  id            bigserial PRIMARY KEY,
  provider      text NOT NULL,
  model         text,
  status        text NOT NULL,        -- healthy | degraded | down
  error_rate    numeric,
  p99_latency_ms int,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Alert history -- every alert fired via send_alert(), Slack-delivered or
-- console-fallback, regardless of sink (ADR-014, ADR-027)
CREATE TABLE alert_history (
  id          bigserial PRIMARY KEY,
  alert_type  text NOT NULL,
  provider    text,
  team_id     text REFERENCES teams(id),
  message     text NOT NULL,
  context     jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);
