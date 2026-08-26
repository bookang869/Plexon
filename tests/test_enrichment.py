from __future__ import annotations

from gateway.config.loader import ContentFilterConfig as GlobalContentFilterConfig
from gateway.config.loader import EnrichmentDefaults
from gateway.enrichment.config import (
    ContentFilterConfig,
    EnrichmentConfig,
    resolve_enrichment_config,
)
from gateway.enrichment.content_filter import check_content_filter
from gateway.enrichment.enrich import enrich_request
from gateway.schemas import ChatCompletionRequest, ChatMessage


def _request(*messages: ChatMessage) -> ChatCompletionRequest:
    return ChatCompletionRequest(model="gpt-4o-mini", messages=list(messages))


# -- enrich_request -----------------------------------------------------


def test_system_prompt_injected_when_no_system_message():
    config = EnrichmentConfig(
        system_prompt="be nice",
        disclaimer=None,
        content_filter=ContentFilterConfig(enabled=True, blocklist=[]),
    )
    request = _request(ChatMessage(role="user", content="hi"))

    result = enrich_request(request, config)

    assert result.messages[0].role == "system"
    assert result.messages[0].content == "be nice"
    assert result.messages[1].content == "hi"
    # original request untouched
    assert len(request.messages) == 1


def test_system_prompt_prepended_when_one_already_exists():
    config = EnrichmentConfig(
        system_prompt="org policy",
        disclaimer=None,
        content_filter=ContentFilterConfig(enabled=True, blocklist=[]),
    )
    request = _request(
        ChatMessage(role="system", content="team prompt"),
        ChatMessage(role="user", content="hi"),
    )

    result = enrich_request(request, config)

    assert len(result.messages) == 2
    assert result.messages[0].role == "system"
    assert result.messages[0].content == "org policy\nteam prompt"


def test_disclaimer_appended_to_last_user_message():
    config = EnrichmentConfig(
        system_prompt=None,
        disclaimer="not legal advice",
        content_filter=ContentFilterConfig(enabled=True, blocklist=[]),
    )
    request = _request(
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="reply"),
        ChatMessage(role="user", content="second"),
    )

    result = enrich_request(request, config)

    assert result.messages[0].content == "first"
    assert result.messages[2].content == "second\nnot legal advice"


# -- resolve_enrichment_config -------------------------------------------


def _global_defaults(system_prompt=None, disclaimer=None, blocklist=None) -> EnrichmentDefaults:
    return EnrichmentDefaults(
        system_prompt=system_prompt,
        disclaimer=disclaimer,
        content_filter=GlobalContentFilterConfig(enabled=True, blocklist=blocklist or []),
    )


def test_team_blocklist_adds_to_global_blocklist():
    global_defaults = _global_defaults(blocklist=["global-term"])
    team_config = {"content_filter": {"blocklist": ["team-term"]}}

    resolved = resolve_enrichment_config(global_defaults, team_config)

    assert resolved.content_filter.blocklist == ["global-term", "team-term"]


def test_team_system_prompt_replaces_global_not_concatenates():
    global_defaults = _global_defaults(system_prompt="global prompt")
    team_config = {"system_prompt": "team prompt"}

    resolved = resolve_enrichment_config(global_defaults, team_config)

    assert resolved.system_prompt == "team prompt"


def test_no_team_override_falls_back_to_global():
    global_defaults = _global_defaults(system_prompt="global prompt", disclaimer="global disclaimer")

    resolved = resolve_enrichment_config(global_defaults, {})

    assert resolved.system_prompt == "global prompt"
    assert resolved.disclaimer == "global disclaimer"


def test_team_content_filter_enabled_override():
    global_defaults = _global_defaults(blocklist=["term"])
    team_config = {"content_filter": {"enabled": False}}

    resolved = resolve_enrichment_config(global_defaults, team_config)

    assert resolved.content_filter.enabled is False
    assert resolved.content_filter.blocklist == ["term"]


# -- check_content_filter -------------------------------------------------


def test_content_filter_substring_match():
    config = ContentFilterConfig(enabled=True, blocklist=["bad word"])
    request = _request(ChatMessage(role="user", content="this has a bad word in it"))

    result = check_content_filter(request, config)

    assert result.blocked is True
    assert result.matched_terms == ["bad word"]


def test_content_filter_case_insensitive():
    config = ContentFilterConfig(enabled=True, blocklist=["BAD WORD"])
    request = _request(ChatMessage(role="user", content="this has a bad word in it"))

    result = check_content_filter(request, config)

    assert result.blocked is True


def test_content_filter_regex_match():
    config = ContentFilterConfig(enabled=True, blocklist=[r"/\bfoo\d+\b/"])
    request = _request(ChatMessage(role="user", content="see foo123 over there"))

    result = check_content_filter(request, config)

    assert result.blocked is True
    assert result.matched_terms == [r"/\bfoo\d+\b/"]


def test_content_filter_no_match():
    config = ContentFilterConfig(enabled=True, blocklist=["nope"])
    request = _request(ChatMessage(role="user", content="totally fine content"))

    result = check_content_filter(request, config)

    assert result.blocked is False
    assert result.matched_terms == []


def test_content_filter_disabled_always_passes():
    config = ContentFilterConfig(enabled=False, blocklist=["bad word"])
    request = _request(ChatMessage(role="user", content="this has a bad word in it"))

    result = check_content_filter(request, config)

    assert result.blocked is False
    assert result.matched_terms == []
