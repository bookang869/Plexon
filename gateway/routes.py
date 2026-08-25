"""Gateway request routes -- TRD §3 steps 1, 2, 5, 6, 7, 8, 10 (receipt,
auth, enrichment, provider selection, call, response translation, delivery).
Steps 3/4 (rate-limit/budget check) and step 9 (spend-ledger + OTel/metrics
logging) are later phases' work and are deliberately absent here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from gateway.auth.team_auth import Team, get_current_team
from gateway.config.loader import get_config
from gateway.enrichment.config import resolve_enrichment_config
from gateway.enrichment.content_filter import check_content_filter
from gateway.enrichment.enrich import enrich_request
from gateway.providers.errors import NonRetryableProviderError, RetryableProviderError
from gateway.providers.registry import UnknownModelError, resolve_provider_for_model
from gateway.schemas import ChatCompletionRequest, ChatCompletionResponse

router = APIRouter()


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    request: ChatCompletionRequest, team: Team = Depends(get_current_team)
) -> ChatCompletionResponse:
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
        return await adapter.chat_completion(enriched_request)
    except UnknownModelError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
