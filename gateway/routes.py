"""Gateway request routes -- TRD §3 steps 1, 2, 3, 4, 5, 6, 7, 8, 9 (partial),
10 (receipt, auth, rate-limit check, budget check, enrichment, provider
selection, call, response translation, spend-ledger write, delivery). The
rest of step 9 (OTel spans/Prometheus metrics) is a later phase's work and is
deliberately absent here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import StreamingResponse

from gateway.auth.team_auth import Team, get_current_team
from gateway.config.loader import get_config
from gateway.enrichment.config import resolve_enrichment_config
from gateway.enrichment.content_filter import check_content_filter
from gateway.enrichment.enrich import enrich_request
from gateway.providers.base import ProviderAdapter
from gateway.providers.errors import NonRetryableProviderError, RetryableProviderError
from gateway.providers.registry import UnknownModelError, resolve_provider_for_model
from gateway.ratelimit.budget import check_budget, compute_cost, record_spend
from gateway.ratelimit.limiter import check_rate_limit, estimate_tokens, reconcile_tpm, resolve_tier
from gateway.redis_client import get_redis
from gateway.resilience.orchestrator import call_with_resilience, resolve_streaming_start
from gateway.schemas import ChatCompletionRequest, ChatCompletionResponse
from gateway.streaming import stream_chat_completion

router = APIRouter()


async def _prepare_request(
    request: ChatCompletionRequest, team: Team
) -> tuple[ChatCompletionRequest, str, ProviderAdapter]:
    """Steps 2, 5, 6 of TRD §3 (allowed-model check, enrichment/content
    filter, provider selection) -- shared by both the streaming and
    non-streaming branches of the handler below.
    """
    if request.model not in team.allowed_models:
        raise HTTPException(status_code=403, detail="model not allowed for this team")

    config = get_config()
    enrichment_config = resolve_enrichment_config(config.enrichment_defaults, team.config)

    filter_result = check_content_filter(request, enrichment_config.content_filter)
    if filter_result.blocked:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "request blocked by content filter",
                "matched_terms": len(filter_result.matched_terms),
            },
        )

    enriched_request = enrich_request(request, enrichment_config)

    try:
        provider_name, adapter = resolve_provider_for_model(enriched_request.model, config)
    except UnknownModelError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return enriched_request, provider_name, adapter


@router.post("/v1/chat/completions", response_model=None)
async def create_chat_completion(
    request: ChatCompletionRequest,
    response: Response,
    team: Team = Depends(get_current_team),
    x_priority: str | None = Header(default=None, alias="X-Priority"),
) -> ChatCompletionResponse | StreamingResponse:
    # TRD §3 step 3 (rate-limit check) runs before step 5/6 (enrichment,
    # provider selection) below -- ADR-020's X-Priority header, defaulting to
    # "realtime", picks the tier; ADR-011's per-tier ceiling is enforced via
    # gateway/ratelimit/limiter.py against the buckets Redis already owns.
    tier = resolve_tier(x_priority, get_config())
    estimated_tokens = estimate_tokens(request)
    decision = await check_rate_limit(team, tier, estimated_tokens)
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    # TRD §3 step 4 (budget check) -- deliberately a 402, distinct from
    # rate-limiting's 429, so callers/tests can tell "out of budget" apart
    # from "sending too fast".
    budget_status = await check_budget(team)
    if budget_status.blocked:
        raise HTTPException(status_code=402, detail="budget exceeded")

    enriched_request, provider_name, adapter = await _prepare_request(request, team)

    async def _record_spend(completed: ChatCompletionResponse, provider: str) -> None:
        cost = compute_cost(completed.usage, provider, completed.model, get_config().pricing)
        await record_spend(team, provider, completed.model, completed.usage, cost, completed.id)

    if enriched_request.stream:
        try:
            serving_provider, _serving_model, chunks, first_chunk = await resolve_streaming_start(
                provider_name, enriched_request.model, enriched_request, get_config(), get_redis()
            )
        except RetryableProviderError as exc:
            # Same reasoning as the non-streaming branch below -- every
            # candidate was exhausted or skipped before producing any output,
            # so nothing has been sent to the client yet and a real status
            # code can still be returned instead of a fake 200.
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            raise HTTPException(status_code=503, detail=str(exc), headers=headers) from exc
        except NonRetryableProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        async def _on_complete(completed: ChatCompletionResponse) -> None:
            await reconcile_tpm(team, tier, estimated_tokens, completed.usage.total_tokens)
            await _record_spend(completed, serving_provider)

        return StreamingResponse(
            stream_chat_completion(chunks, first_chunk, enriched_request, on_complete=_on_complete),
            media_type="text/event-stream",
            headers={"X-Budget-Warning": "true"} if budget_status.warning else None,
        )

    try:
        serving_provider, completion = await call_with_resilience(
            enriched_request, provider_name, enriched_request.model, adapter, get_config(), get_redis()
        )
    except RetryableProviderError as exc:
        # Every candidate in the primary+fallback chain was exhausted or
        # skipped (breaker open) -- surface as 503 so callers know it's worth
        # retrying.
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(status_code=503, detail=str(exc), headers=headers) from exc
    except NonRetryableProviderError as exc:
        # Upstream rejected our forwarded request outright (auth failure,
        # content policy) -- not something a retry would fix.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await reconcile_tpm(team, tier, estimated_tokens, completion.usage.total_tokens)
    await _record_spend(completion, serving_provider)
    if budget_status.warning:
        response.headers["X-Budget-Warning"] = "true"
    return completion


@router.get("/v1/models")
async def list_models(team: Team = Depends(get_current_team)) -> dict:
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in team.allowed_models]}
