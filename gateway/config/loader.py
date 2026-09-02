"""Loads gateway.yaml global config into a validated Pydantic model, with
file-watch hot reload (ADR-005). Per-team, frequently-changed settings do not
belong here -- they live in Postgres, edited via the admin API.
"""

from __future__ import annotations

import asyncio
import logging
import os

import yaml
from pydantic import BaseModel
from watchfiles import awatch

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "./config.yaml"


class ProviderConfig(BaseModel):
    base_url: str
    models: list[str]


class ProvidersConfig(BaseModel):
    openai: ProviderConfig
    anthropic: ProviderConfig
    ollama: ProviderConfig


class FallbackChainsConfig(BaseModel):
    fast_tier: list[str]
    frontier_tier: list[str]


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int
    window_seconds: int
    cooldown_seconds: int


class HealthCheckConfig(BaseModel):
    interval_seconds: int


class PriorityTierConfig(BaseModel):
    rpm_ceiling_pct: int


class ContentFilterConfig(BaseModel):
    enabled: bool
    blocklist: list[str] = []


class EnrichmentDefaults(BaseModel):
    system_prompt: str | None = None
    disclaimer: str | None = None
    content_filter: ContentFilterConfig


class ModelPricing(BaseModel):
    input_per_1k: float
    output_per_1k: float


class PricingConfig(BaseModel):
    openai: dict[str, ModelPricing]
    anthropic: dict[str, ModelPricing]
    ollama: dict[str, ModelPricing]


class AlertingConfig(BaseModel):
    error_rate_threshold: float
    latency_p99_ms_threshold: int
    evaluator_interval_seconds: int


class GatewayConfig(BaseModel):
    providers: ProvidersConfig
    fallback_chains: FallbackChainsConfig
    circuit_breaker: CircuitBreakerConfig
    health_check: HealthCheckConfig
    priority_tiers: dict[str, PriorityTierConfig]
    enrichment_defaults: EnrichmentDefaults
    pricing: PricingConfig
    alerting: AlertingConfig


_config: GatewayConfig | None = None
_watch_task: asyncio.Task | None = None


def _config_path() -> str:
    return os.environ.get("PLEXON_CONFIG_PATH", DEFAULT_CONFIG_PATH)


def _load_from_disk() -> GatewayConfig:
    with open(_config_path()) as f:
        raw = yaml.safe_load(f)
    return GatewayConfig.model_validate(raw)


def get_config() -> GatewayConfig:
    if _config is None:
        raise RuntimeError("config not loaded yet; call start_config_watcher() first")
    return _config


def reload_config() -> None:
    """Loads config from disk and swaps it in. Raises on parse/validation
    failure -- callers decide whether to swallow that (the background
    file-watch loop, which logs and keeps the previous config) or propagate
    it (the manual /admin/config/reload endpoint, which should tell the
    admin the reload failed rather than silently no-op'ing).
    """
    global _config
    _config = _load_from_disk()


async def _watch_loop() -> None:
    async for _ in awatch(_config_path()):
        try:
            reload_config()
        except Exception:
            logger.exception("failed to reload config from %s; keeping previous config", _config_path())


def start_config_watcher() -> None:
    global _watch_task
    reload_config()
    _watch_task = asyncio.create_task(_watch_loop())
