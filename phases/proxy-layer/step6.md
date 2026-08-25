# Step 6: gateway-routing

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/TRD.md` — §3 (Request Flow — this phase implements steps 1, 2, 5, 6, 7, 8, 10 of the numbered flow; **not** steps 3/4 rate-limit/budget checks, and step 9 logging is reduced to nothing yet since spend-ledger writes and OTel/Prometheus don't exist until later phases), §5 (YAML `fallback_chains` — read but note this phase does **not** implement fallback, only direct model→provider resolution), §6.1 (Gateway API spec — `POST /v1/chat/completions`, `GET /v1/models`)
- `/docs/ARCHITECTURE.md` — "Data Flow" diagram
- `phases/proxy-layer/step1.md`'s actual output: `gateway/schemas.py`
- `phases/proxy-layer/step3.md`'s actual output: `gateway/providers/` — `ProviderAdapter`, `OpenAIAdapter`, `AnthropicAdapter`, `OllamaAdapter`, and the `RetryableProviderError`/`NonRetryableProviderError` exception types
- `phases/proxy-layer/step4.md`'s actual output: `gateway/auth/team_auth.py` — `get_current_team` dependency, `Team` model
- `phases/proxy-layer/step5.md`'s actual output: `gateway/enrichment/` — `resolve_enrichment_config`, `enrich_request`, `check_content_filter`
- `phases/proxy-layer/step0.md`'s actual output: `gateway/main.py` (the `/healthz`-only app this step adds routes to), `gateway/config/loader.py` (`get_config()` — for `providers.<name>.models` and `enrichment_defaults`)

## Task

Wire everything built so far into the actual gateway request path: `POST /v1/chat/completions` (non-streaming only — `stream: true` is step 7) and `GET /v1/models`.

### Provider resolution (`gateway/providers/registry.py` or similar)

```python
def resolve_provider_for_model(model: str, config: GatewayConfig) -> ProviderAdapter: ...
```

Look up which provider serves the requested `model` by scanning `config.providers.<name>.models` (from YAML, step 0) for a match; instantiate/return the corresponding adapter (`OpenAIAdapter`/`AnthropicAdapter`/`OllamaAdapter` from step 3). If no provider lists that model, raise a `404`-mapped error (unknown model) — this is a request-shape problem, not a provider failure, so it must **not** be one of step 3's `ProviderError` subclasses.

This function does one static lookup, nothing more:
- **No fallback-chain walking.** `config.fallback_chains` exists in the loaded config (step 0) but is not consulted here — resolving a model to its single configured provider is all this phase does. Fallback logic is the `resilience` phase.
- **No retry.** If the resolved adapter's call fails, this phase does not retry it — it returns whatever the adapter raised, translated to an HTTP error. Retry/backoff is also the `resilience` phase.
- Adapter instances can be constructed once at app startup (they're stateless HTTP clients pointed at fixed base URLs) and reused — don't re-instantiate per request.

### Routes (`gateway/main.py`, or split into `gateway/routes.py` and imported — your call, keep `main.py` from becoming a dumping ground if it's getting long)

`POST /v1/chat/completions`:
1. `team: Team = Depends(get_current_team)` (step 4).
2. Parse body into `ChatCompletionRequest` (step 1). If `request.model not in team.allowed_models` → `403` (team isn't allowed this model — this check belongs here since it's about authorization, not provider resolution).
3. `resolve_enrichment_config` + `check_content_filter` (step 5) — if blocked, `400` with `matched_terms` info (don't leak the actual blocklist entries in the response, just confirm it was blocked).
4. `enrich_request` (step 5).
5. `resolve_provider_for_model` (above), then call `adapter.chat_completion(enriched_request)`.
6. On `RetryableProviderError`/`NonRetryableProviderError` from step 3 → map to an appropriate HTTP status (e.g. `503` for retryable/upstream-unavailable, `502` or `422` for non-retryable, your judgement — document the mapping in a docstring since later phases' retry/fallback logic will change what happens before this mapping is even reached).
7. Return the `ChatCompletionResponse` as-is.

`GET /v1/models`:
1. `team: Team = Depends(get_current_team)`.
2. Return `{"data": [{"id": m, "object": "model"} for m in team.allowed_models]}` (OpenAI's `/v1/models` shape is close to this — check `docs/TRD.md` §6.1, it doesn't over-specify the response body, so a reasonable OpenAI-like shape is fine).

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build postgres mock-openai mock-anthropic
uv run pytest tests/test_routing.py -v
# tests/test_routing.py must cover, using the seed-team fixture from step 4 and the real mock services:
#   - valid request to an allowed model -> 200, correct ChatCompletionResponse
#   - request for a model not in team.allowed_models -> 403
#   - request for a model no provider serves -> 404
#   - request with content matching the blocklist -> 400
#   - missing/invalid auth -> 401 (reusing step 4's behavior through the real route)
#   - GET /v1/models returns exactly the team's allowed_models
docker compose -f deploy/docker-compose.yml down
```

## Verification Procedure

1. Run the AC commands above.
2. Check the architecture checklist:
   - Does the request flow match TRD §3's steps 1/2/5/6/7/8/10, explicitly skipping 3/4 (rate-limit/budget) and simplifying 9 (logging) — with nothing added that belongs to a later phase?
   - Is `resolve_provider_for_model` free of fallback-chain or retry logic?
   - Are provider adapter instances created once, not per-request?
3. Based on the result, update `phases/proxy-layer/index.json` step 6:
   - Success → `"status": "completed"`, `"summary": "one-line summary — routes added, resolution function, for the streaming step to extend"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't implement rate-limiting or budget checks, even as a stub. Reason: explicitly out of scope for `proxy-layer` (TRD §12) — adding even a no-op stub here creates a code path later phases have to find and replace rather than add to cleanly.
- Don't walk `fallback_chains` or retry a failed provider call. Reason: `resilience` phase's job — this phase proves the direct single-provider path works end-to-end first.
- Don't write spend-ledger rows or emit OTel/Prometheus data. Reason: `spend_ledger` writes belong to `ratelimit-budget` (once budget tracking exists, the write becomes meaningful); OTel/Prometheus belong to `observability`. Adding either now means later phases modify this step's code instead of adding their own.
- Do not break existing tests.
