"""Tests for gateway/resilience/fallback.py -- pure function, no I/O, no
Redis/Postgres required.
"""

from __future__ import annotations

import os

import yaml

from gateway.config.loader import GatewayConfig
from gateway.resilience.fallback import resolve_fallback_chain

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "test_config.yaml")

with open(_CONFIG_PATH) as f:
    _CONFIG = GatewayConfig.model_validate(yaml.safe_load(f))


def test_mid_chain_entry_returns_remaining_entries_in_order():
    # fast_tier: [anthropic:claude-sonnet, openai:gpt-4o-mini, ollama:llama3]
    result = resolve_fallback_chain("anthropic", "claude-sonnet", _CONFIG)
    assert result == [("openai", "gpt-4o-mini"), ("ollama", "llama3")]


def test_frontier_tier_entry_does_not_pick_up_fast_tier_entries():
    # frontier_tier: [anthropic:claude-opus, openai:gpt-4o]
    result = resolve_fallback_chain("anthropic", "claude-opus", _CONFIG)
    assert result == [("openai", "gpt-4o")]


def test_pair_not_listed_in_any_chain_returns_empty_list():
    assert resolve_fallback_chain("openai", "gpt-3.5-turbo", _CONFIG) == []


def test_last_entry_in_a_chain_returns_empty_list():
    assert resolve_fallback_chain("ollama", "llama3", _CONFIG) == []
    assert resolve_fallback_chain("openai", "gpt-4o", _CONFIG) == []
