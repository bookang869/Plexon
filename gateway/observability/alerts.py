"""Alert sink (PRD Core Feature 4, ADR-014, ADR-027). One small function,
called from four trigger points: circuit breaker open
(gateway/resilience/circuit_breaker.py), budget crossing 80%
(gateway/ratelimit/budget.py), and provider error-rate/latency-P99 breach
(gateway/observability/alert_evaluator.py). POSTs to Slack if
SLACK_WEBHOOK_URL is set; otherwise logs at WARNING (console/file fallback).
Never raises -- a broken webhook must not break request handling or the
evaluator loop.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from gateway.db import get_pool

logger = logging.getLogger(__name__)

_INSERT_ALERT_HISTORY_ROW = """
    INSERT INTO alert_history (alert_type, provider, team_id, message, context)
    VALUES ($1, $2, $3, $4, $5)
"""


async def send_alert(alert_type: str, message: str, context: dict) -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook_url:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(webhook_url, json={"text": f"[{alert_type}] {message}"})
                resp.raise_for_status()
        except Exception:
            logger.exception("failed to POST alert to Slack webhook: alert_type=%s", alert_type)
    else:
        logger.warning("ALERT [%s]: %s (context=%s)", alert_type, message, context)

    try:
        await get_pool().execute(
            _INSERT_ALERT_HISTORY_ROW,
            alert_type,
            context.get("provider"),
            context.get("team_id"),
            message,
            json.dumps(context),
        )
    except Exception:
        logger.exception("failed to write alert_history row for alert_type=%s", alert_type)
