"""Gateway request routes -- TRD §3 steps 1, 2, 5, 6, 7, 8, 10 (receipt,
auth, enrichment, provider selection, call, response translation, delivery).
Steps 3/4 (rate-limit/budget check) and step 9 (spend-ledger + OTel/metrics
logging) are later phases' work and are deliberately absent here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from gateway.auth.team_auth import Team, get_current_team
from gateway.config.loader import get_config
from gateway.enrichment.config import resolve_enrichment_config
from gateway.enrichment.content_filter import check_content_filter
from gateway.enrichment.enrich import enrich_request
from gateway.providers.base import ProviderAdapter
from gateway.providers.errors import NonRetryableProviderError, RetryableProviderError
from gateway.providers.registry import UnknownModelError, resolve_provider_for_model
from gateway.schemas import ChatCompletionRequest, ChatCompletionResponse
from gateway.streaming import stream_chat_completion

router = APIRouter()


async def _prepare_request(
    request: ChatCompletionRequest, team: Team
) -> tuple[ChatCompletionRequest, ProviderAdapter]:
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
        adapter = resolve_provider_for_model(enriched_request.model, config)
    except UnknownModelError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return enriched_request, adapter


@router.post("/v1/chat/completions", response_model=None)
async def create_chat_completion(
    request: ChatCompletionRequest, team: Team = Depends(get_current_team)
) -> ChatCompletionResponse | StreamingResponse:
    enriched_request, adapter = await _prepare_request(request, team)

    if enriched_request.stream:
        return StreamingResponse(
            stream_chat_completion(adapter, enriched_request),
            media_type="text/event-stream",
        )

    try:
        return await adapter.chat_completion(enriched_request)
    except RetryableProviderError as exc:
        # Transient upstream failure (timeout, 429, 5xx) -- the resilience
        # phase will retry/fall back before this mapping is ever reached;
        # for now surface it as 503 so callers know it's worth retrying.
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(status_code=503, detail=str(exc), headers=headers) from exc
    except NonRetryableProviderError as exc:
        # Upstream rejected our forwarded request outright (auth failure,
        # content policy) -- not something a retry would fix.
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/v1/models")
async def list_models(team: Team = Depends(get_current_team)) -> dict:
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in team.allowed_models]}
