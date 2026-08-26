"""Stateless, per-request fault-injection trigger shared by mock-openai and
mock-anthropic (ADR-025). Every request carries its own fault trigger via the
`X-Mock-Fault` header or a `--fault-<type>` model-name suffix -- there is no
global toggle or shared mutable state, so concurrent requests with different
triggers never interfere with each other.
"""

from __future__ import annotations

import asyncio

from fastapi.responses import JSONResponse

FAULT_TYPES = ("timeout", "error", "rate_limit")
_MAGIC_SUFFIX_PREFIX = "--fault-"

TIMEOUT_FAULT_SECONDS = 30


def extract_fault_and_model(model: str, header_value: str | None) -> tuple[str | None, str]:
    """Return (fault_type_or_None, model_name_with_magic_suffix_stripped)."""
    if header_value in FAULT_TYPES:
        return header_value, model
    for fault_type in FAULT_TYPES:
        suffix = f"{_MAGIC_SUFFIX_PREFIX}{fault_type}"
        if model.endswith(suffix):
            return fault_type, model[: -len(suffix)]
    return None, model


async def apply_fault(fault_type: str | None) -> JSONResponse | None:
    """If a fault is triggered, act on it. Returns a response the caller
    should return immediately, or None if the caller should proceed normally.
    """
    if fault_type is None:
        return None
    if fault_type == "timeout":
        await asyncio.sleep(TIMEOUT_FAULT_SECONDS)
        return None
    if fault_type == "error":
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "mock provider fault: internal error", "type": "mock_fault"}},
        )
    if fault_type == "rate_limit":
        return JSONResponse(
            status_code=429,
            content={"error": {"message": "mock provider fault: rate limited", "type": "mock_fault"}},
            headers={"Retry-After": "1"},
        )
    raise ValueError(f"unknown fault type: {fault_type}")
