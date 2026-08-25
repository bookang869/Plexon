"""Rule-based content filtering (ADR-015: keyword/regex only, no ML or
external moderation API). A pure decision function -- it only detects a
violation; the caller (gateway-routing, step 6) decides what HTTP response
a blocked result produces.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from gateway.enrichment.config import ContentFilterConfig
from gateway.schemas import ChatCompletionRequest


class ContentFilterResult(BaseModel):
    blocked: bool
    matched_terms: list[str]


def _matches(term: str, content: str) -> bool:
    """A blocklist entry wrapped in slashes (e.g. "/foo\\d+/") is treated as
    a regex; any other entry is matched as a case-insensitive substring.
    """
    if len(term) >= 2 and term.startswith("/") and term.endswith("/"):
        return re.search(term[1:-1], content, re.IGNORECASE) is not None
    return term.lower() in content.lower()


def check_content_filter(
    request: ChatCompletionRequest, config: ContentFilterConfig
) -> ContentFilterResult:
    if not config.enabled:
        return ContentFilterResult(blocked=False, matched_terms=[])

    matched_terms = [
        term
        for term in config.blocklist
        if any(_matches(term, message.content) for message in request.messages)
    ]

    return ContentFilterResult(blocked=bool(matched_terms), matched_terms=matched_terms)
