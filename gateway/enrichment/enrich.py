"""System-prompt/disclaimer injection into an incoming chat request, per the
resolved `EnrichmentConfig` (global YAML defaults merged with per-team
overrides, see `config.py`).
"""

from __future__ import annotations

from gateway.enrichment.config import EnrichmentConfig
from gateway.schemas import ChatCompletionRequest, ChatMessage


def enrich_request(request: ChatCompletionRequest, config: EnrichmentConfig) -> ChatCompletionRequest:
    messages = list(request.messages)

    if config.system_prompt:
        system_index = next((i for i, m in enumerate(messages) if m.role == "system"), None)
        if system_index is None:
            messages.insert(0, ChatMessage(role="system", content=config.system_prompt))
        else:
            existing = messages[system_index]
            messages[system_index] = ChatMessage(
                role="system",
                content=f"{config.system_prompt}\n{existing.content}",
            )

    if config.disclaimer:
        user_index = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"),
            None,
        )
        if user_index is not None:
            existing = messages[user_index]
            messages[user_index] = ChatMessage(
                role="user",
                content=f"{existing.content}\n{config.disclaimer}",
            )

    return request.model_copy(update={"messages": messages})
