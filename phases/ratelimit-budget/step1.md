# Step 1: priority-tiers

## Files to read

First read the following files to understand the project's architecture and design intent:

- `/docs/ARCHITECTURE.md` — "Request Metadata" section (priority tier via `X-Priority` header, not a body field)
- `/docs/ADR.md` — ADR-011 (tiered rate limiting: per-tier ceilings, not a queue; requests exceeding their tier's ceiling are rejected immediately with 429, never held/queued) and ADR-020 (`X-Priority: realtime|batch` header, defaults to `realtime`, keeps the request body strictly OpenAI-compatible)
- `/docs/TRD.md` — §3 steps 3 ("Rate-limit check" — reads `X-Priority`, Redis token-bucket check at that tier's ceiling, `429` + `Retry-After` on breach) and §4.2 (Redis key patterns `ratelimit:{team_id}:{tier}:rpm` / `:tpm`)
- `phases/ratelimit-budget/step0.md`'s actual output: `gateway/ratelimit/token_bucket.py` (`check_and_consume`, `refund`, `BucketResult` — import these directly, don't reimplement) and `gateway/redis_client.py` (`get_redis()`)
- `gateway/config/loader.py` — `GatewayConfig.priority_tiers: dict[str, PriorityTierConfig]` already exists (`PriorityTierConfig.rpm_ceiling_pct: int`), loaded from `config.yaml`'s `priority_tiers` section (currently `realtime: {rpm_ceiling_pct: 100}`, `batch: {rpm_ceiling_pct: 60}`)
- `gateway/auth/team_auth.py` — the `Team` model (`rpm_limit`, `tpm_limit` fields already present, sourced from the `teams` table)
- `gateway/routes.py` — the existing `create_chat_completion` handler and `_prepare_request` helper you'll extend
- `gateway/streaming.py` — `stream_chat_completion`'s `on_complete` callback parameter, which delivers the fully-assembled `ChatCompletionResponse` (including `usage`) once a stream finishes. This is your hook for tpm reconciliation on the streaming path.
- `gateway/schemas.py` — `ChatCompletionRequest`, `Usage`

Read carefully through the code produced in previous steps, understand the design intent, and then start working.

## Task

Wire tiered rate limiting into the request path, on top of step 0's generic bucket primitive.

### Design decision already made (don't re-litigate)

The YAML config only defines one ceiling percentage per tier (`rpm_ceiling_pct`), not separate rpm/tpm ceilings. Reuse the same percentage for both: a tier's tpm capacity is `team.tpm_limit * ceiling_pct / 100`, exactly like rpm. Do not add a new `tpm_ceiling_pct` config field — that would require a YAML schema change beyond what's documented, for no behavioral benefit the demo needs.

### `gateway/ratelimit/limiter.py`

```python
def resolve_tier(x_priority: str | None, config: GatewayConfig) -> str:
    """Defaults to 'realtime' when the header is absent. Raises HTTPException(400)
    for a tier name not present in config.priority_tiers -- don't silently fall
    back to 'realtime' for a typo'd/unknown tier, that would mask a caller bug."""

def estimate_tokens(request: ChatCompletionRequest) -> int:
    """Pre-call best-effort estimate of total tokens this request will consume
    (prompt + completion), used only to reserve tpm-bucket capacity before the
    real usage is known. Base it on message content length plus request.max_tokens
    (or a reasonable default ceiling if max_tokens is unset) -- document the
    heuristic in a docstring since it's a genuine approximation, not exact."""

class RateLimitDecision(BaseModel):
    allowed: bool
    retry_after_seconds: float | None
    tier: str
    estimated_tokens: int  # needed by reconcile_tpm afterward

async def check_rate_limit(team: Team, tier: str, estimated_tokens: int) -> RateLimitDecision:
    """Checks the tier's rpm bucket (cost=1) at key ratelimit:{team.id}:{tier}:rpm,
    then the tpm bucket (cost=estimated_tokens) at ratelimit:{team.id}:{tier}:tpm.
    Both must have room for the request to be admitted. See rollback rule below."""

async def reconcile_tpm(team: Team, tier: str, estimated_tokens: int, actual_tokens: int) -> None:
    """Called after the real response is known. If actual < estimated, refund
    the difference (the tpm bucket was over-charged). If actual > estimated,
    the shortfall is silently absorbed -- a response has already been delivered,
    there is no way to retroactively deny it, so just let the bucket run slightly
    tighter until the next natural refill. Don't raise or log this as an error."""
```

**Core rule — rollback on partial denial.** `check_rate_limit` deducts from the rpm bucket, then the tpm bucket. If the rpm check passes but the tpm check fails, you must call `refund` on the rpm bucket before returning `allowed=False` — otherwise a denied request still permanently consumes a unit of rpm capacity, which is wrong. A brief window exists where two concurrent requests could each pass rpm and then race on tpm in a way a single merged two-key Lua script would avoid — that's a known, accepted tradeoff for this portfolio-scope project (ADR-002); don't build a bespoke dual-key Lua script to close it, the check-then-rollback-on-failure approach using step 0's existing primitive is enough.

### Wiring into `gateway/routes.py`

Add an `x_priority: str | None = Header(default=None, alias="X-Priority")` parameter to `create_chat_completion`. Immediately after the `get_current_team` dependency resolves (before `_prepare_request`'s enrichment/content-filter/provider-selection logic — TRD §3 orders rate-limit check at step 3, before enrichment at step 5), call `resolve_tier` + `estimate_tokens` + `check_rate_limit`. On denial, raise `HTTPException(status_code=429, detail=..., headers={"Retry-After": str(decision.retry_after_seconds)})`.

After a successful response:
- **Non-streaming**: once `adapter.chat_completion(...)` returns, call `reconcile_tpm` with `response.usage.total_tokens` as `actual_tokens`.
- **Streaming**: pass an `on_complete` callback into `stream_chat_completion` that calls `reconcile_tpm` with the assembled response's `usage.total_tokens`.

Don't reconcile (or even attempt to) if the provider call raised an error before producing any usage — there's nothing to reconcile against; the pre-call estimate deduction simply stands as the cost of the failed attempt.

## Acceptance Criteria

```bash
uv run ruff check .
docker compose -f deploy/docker-compose.yml up -d --build redis postgres mock-openai mock-anthropic
uv run pytest tests/test_priority_tiers.py tests/test_routing.py tests/test_streaming.py -v
docker compose -f deploy/docker-compose.yml down
```

`tests/test_priority_tiers.py` must cover, using the `seeded_team` fixture and real mocks/Redis/Postgres:
- A request within a team's rpm limit succeeds.
- Exceeding a team's rpm limit for a tier returns `429` with a `Retry-After` header.
- `batch` tier's ceiling is lower than `realtime`'s for the same team (per `config.yaml`'s `rpm_ceiling_pct` values) — a burst that `realtime` tolerates gets rejected under `batch` at the same raw request count.
- An unknown `X-Priority` value returns `400`, not a silent fallback to `realtime`.
- A denied tpm check (e.g. seed a team with a very low `tpm_limit` and send a request with a large `max_tokens`) does not leave the rpm bucket permanently short by one — verify a subsequent request within rpm's own limit still succeeds (proves the rollback rule works).
- Non-streaming and streaming requests both correctly reconcile the tpm bucket afterward (verify via a follow-up rate-limit check showing more remaining capacity than the pre-call estimate would have left, when actual usage came in lower).

## Verification Procedure

1. Run the AC commands above, and re-run `tests/test_routing.py`/`tests/test_streaming.py` to confirm no regression from steps 6/7 of `proxy-layer`.
2. Check the architecture checklist:
   - Is priority signaled only via the `X-Priority` header, never a body field (ADR-020)?
   - Are denied requests over their tier's ceiling rejected immediately (429), never queued or delayed (ADR-011)?
   - Does all rate-limit state live only in Redis, never gateway-process memory (ADR-007)?
3. Based on the result, update `phases/ratelimit-budget/index.json` step 1:
   - Success → `"status": "completed"`, `"summary": "one-line summary — files/functions added, the rollback-on-partial-denial mechanism, where reconciliation is hooked into streaming vs non-streaming"`
   - Still failing after 3 fix attempts → `"status": "error"`, `"error_message": "specific error details"`
   - User intervention needed → `"status": "blocked"`, `"blocked_reason": "specific reason"`, then stop immediately

## Prohibited

- Don't add a `tpm_ceiling_pct` (or similarly new) YAML config field. Reason: already decided above — reuse `rpm_ceiling_pct` for both dimensions.
- Don't build a merged dual-key Lua script to close the rpm/tpm race window. Reason: explicitly accepted as out of scope for this project's rigor level (ADR-002); the check-then-rollback approach is sufficient.
- Don't retroactively fail or log an error when `actual_tokens > estimated_tokens` during reconciliation. Reason: the response has already been delivered to the caller; there's nothing left to enforce against for this request.
- Don't duplicate the auth/enrichment/content-filter/provider-selection logic already in `routes.py`. Reason: single source of truth, established in the `proxy-layer` phase.
- Do not break existing tests, especially `tests/test_routing.py` and `tests/test_streaming.py`.
