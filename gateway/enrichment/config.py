"""Merges global enrichment defaults (config.yaml, ADR-005) with a team's
per-team overrides (`teams.config` jsonb, ADR-021) into one resolved config
that `enrich.py` and `content_filter.py` consume.
"""

from __future__ import annotations

from pydantic import BaseModel

from gateway.config.loader import EnrichmentDefaults


class ContentFilterConfig(BaseModel):
    enabled: bool
    blocklist: list[str]


class EnrichmentConfig(BaseModel):
    system_prompt: str | None
    disclaimer: str | None
    content_filter: ContentFilterConfig


def resolve_enrichment_config(global_defaults: EnrichmentDefaults, team_config: dict) -> EnrichmentConfig:
    """Merge semantics (ADR-021):

    - `system_prompt` / `disclaimer`: a team override replaces the global
      default entirely when present -- never concatenated.
    - `content_filter.blocklist`: team entries are added to the global
      blocklist (union), so a team can only tighten filtering, never lose
      the org-wide baseline.
    - `content_filter.enabled`: a team override replaces the global value
      when present.
    """
    team_filter = team_config.get("content_filter") or {}

    blocklist = list(global_defaults.content_filter.blocklist)
    for term in team_filter.get("blocklist", []):
        if term not in blocklist:
            blocklist.append(term)

    return EnrichmentConfig(
        system_prompt=team_config.get("system_prompt", global_defaults.system_prompt),
        disclaimer=team_config.get("disclaimer", global_defaults.disclaimer),
        content_filter=ContentFilterConfig(
            enabled=team_filter.get("enabled", global_defaults.content_filter.enabled),
            blocklist=blocklist,
        ),
    )
