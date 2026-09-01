"""Fallback-chain resolution (TRD §3 step 6, PRD Core Feature 3). Pure
function, no I/O -- searches the static YAML fallback_chains config, not
provider health.
"""

from __future__ import annotations

from gateway.config.loader import GatewayConfig


def resolve_fallback_chain(provider: str, model: str, config: GatewayConfig) -> list[tuple[str, str]]:
    """Search every tier in `config.fallback_chains` for a `"{provider}:{model}"`
    entry and return the remaining entries after that position, split into
    (provider, model) tuples. Degrades from where the request already is --
    never restarts from the top of the chain, which would silently route to a
    pricier, unbudgeted model. Returns [] if the pair isn't listed anywhere.
    """
    target = f"{provider}:{model}"
    for chain in (config.fallback_chains.fast_tier, config.fallback_chains.frontier_tier):
        if target not in chain:
            continue
        remaining = chain[chain.index(target) + 1 :]
        return [tuple(entry.split(":", 1)) for entry in remaining]
    return []
