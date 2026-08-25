"""Static model -> provider resolution (TRD §3 step 6, gateway-routing).
Scans each provider's configured `models` list (YAML) for the requested
model and returns its adapter. No fallback-chain walking, no retry -- both
belong to the resilience phase; this phase only proves the direct
single-provider path works end-to-end.
"""

from __future__ import annotations

from gateway.config.loader import GatewayConfig
from gateway.providers.anthropic_adapter import AnthropicAdapter
from gateway.providers.base import ProviderAdapter
from gateway.providers.ollama_adapter import OllamaAdapter
from gateway.providers.openai_adapter import OpenAIAdapter

_ADAPTER_CLASSES = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "ollama": OllamaAdapter,
}

_adapters: dict[str, ProviderAdapter] = {}


class UnknownModelError(Exception):
    """Raised when no configured provider lists the requested model. This is
    a request-shape problem (maps to 404), not a provider failure -- kept
    deliberately outside the ProviderError hierarchy so it can't be mistaken
    for a retryable/non-retryable upstream failure.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"no provider serves model {model!r}")


def _get_adapter(name: str, base_url: str) -> ProviderAdapter:
    if name not in _adapters:
        _adapters[name] = _ADAPTER_CLASSES[name](base_url)
    return _adapters[name]


def resolve_provider_for_model(model: str, config: GatewayConfig) -> ProviderAdapter:
    for name in _ADAPTER_CLASSES:
        provider_config = getattr(config.providers, name)
        if model in provider_config.models:
            return _get_adapter(name, provider_config.base_url)
    raise UnknownModelError(model)
