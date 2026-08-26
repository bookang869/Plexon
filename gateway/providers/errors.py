"""Exception types shared by all provider adapters. The retryable/
non-retryable split is what the resilience phase's retry-then-fallback logic
switches on (CLAUDE.md, TRD §12): retry the primary provider only on
retryable errors (timeouts, rate limits), fall back immediately otherwise.
Getting this classification wrong here means re-auditing every adapter later.
"""

from __future__ import annotations

import httpx

_NON_RETRYABLE_STATUS_CODES = {400, 401, 403}


class ProviderError(Exception):
    """Base exception for all provider adapter failures."""


class RetryableProviderError(ProviderError):
    """Timeouts, rate limits (429), and other transient/infra failures."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NonRetryableProviderError(ProviderError):
    """Auth failures (401/403) and content-policy/bad-request (400) errors."""


def raise_for_provider_status(resp: httpx.Response, provider: str) -> None:
    """Translate an HTTP response's status code into the retryable/
    non-retryable split. Returns silently on success (< 400).
    """
    if resp.status_code < 400:
        return
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After")
        raise RetryableProviderError(
            f"{provider} rate limited",
            retry_after=float(retry_after) if retry_after else None,
        )
    if resp.status_code in _NON_RETRYABLE_STATUS_CODES:
        raise NonRetryableProviderError(
            f"{provider} request rejected ({resp.status_code}): {resp.text}"
        )
    raise RetryableProviderError(f"{provider} request failed ({resp.status_code}): {resp.text}")


def wrap_transport_error(exc: httpx.HTTPError, provider: str) -> RetryableProviderError:
    """Network-level failures (timeout, connection refused) are transient
    infra failures -- worth a retry.
    """
    return RetryableProviderError(f"{provider} transport error: {exc}")
