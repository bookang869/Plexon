"""Runnable demo: a request routed to the primary provider, the provider
failing, the circuit breaker opening, and the fallback provider serving the
response successfully -- then the breaker recovering after its cooldown.

Hits a running gateway over HTTP only (no direct Redis/Postgres access), so
it exercises exactly what a real caller would see. Requires the full stack
up and demo teams seeded:

    docker compose -f deploy/docker-compose.yml up -d --build
    python3 scripts/setup_demo_teams.py
    python3 scripts/demo_circuit_breaker_failover.py

Uses the `demo-realtime-highvolume` team (the only seeded team whose
`allowed_models` includes the fault-injection model) and the mock-anthropic
fault-injection magic suffix from ADR-025: `claude-sonnet--fault-error`
always returns an instant HTTP 500, classified as a retryable provider
error (gateway/providers/errors.py) -- the same mechanism benchmarks/
bench_failover.py uses.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

BASE_URL = "http://localhost:8000"
TEAM_ID = "demo-realtime-highvolume"
FAULT_MODEL = "claude-sonnet--fault-error"
HEALTHY_MODEL = "claude-sonnet"
FALLBACK_MODEL = "gpt-4o-mini"
FAILURE_THRESHOLD = 5  # config.yaml circuit_breaker.failure_threshold
COOLDOWN_SECONDS = 30  # config.yaml circuit_breaker.cooldown_seconds

_DEMO_TEAMS_PATH = Path(__file__).parent / "demo_teams.json"


def _load_api_key(team_id: str = TEAM_ID, demo_teams_path: Path = _DEMO_TEAMS_PATH) -> str:
    teams = json.loads(demo_teams_path.read_text())
    for team in teams:
        if team["team_id"] == team_id:
            return team["api_key"]
    raise SystemExit(f"team {team_id!r} not found in {demo_teams_path} -- run setup_demo_teams.py first")


def _metric(text: str, name: str, labels: str) -> float:
    match = re.search(rf'^{name}\{{{re.escape(labels)}\}} ([0-9.e+-]+)$', text, re.MULTILINE)
    return float(match.group(1)) if match else 0.0


def _breaker_snapshot(client: httpx.Client) -> dict:
    text = client.get("/metrics", follow_redirects=True).text
    return {
        "state": _metric(text, "gateway_circuit_breaker_state", 'provider="anthropic"'),
        "opened": _metric(text, "gateway_circuit_breaker_transitions_total", 'from_state="closed",provider="anthropic",to_state="open"'),
        "half_opened": _metric(text, "gateway_circuit_breaker_transitions_total", 'from_state="open",provider="anthropic",to_state="half_open"'),
        "closed": _metric(text, "gateway_circuit_breaker_transitions_total", 'from_state="half_open",provider="anthropic",to_state="closed"'),
        "fallback_count": _metric(text, "gateway_fallback_triggered_total", 'from_provider="anthropic",to_provider="openai"'),
    }


_STATE_NAMES = {0.0: "closed", 1.0: "half_open", 2.0: "open"}


def _send(client: httpx.Client, headers: dict, model: str) -> tuple[int, str | None, float]:
    payload = {"model": model, "messages": [{"role": "user", "content": "demo request"}]}
    started = time.monotonic()
    resp = client.post("/v1/chat/completions", json=payload, headers=headers, timeout=30.0)
    elapsed_ms = (time.monotonic() - started) * 1000
    served_by = resp.json().get("model") if resp.status_code == 200 else None
    return resp.status_code, served_by, elapsed_ms


def main() -> None:
    headers = {"Authorization": f"Bearer {_load_api_key()}"}
    client = httpx.Client(base_url=BASE_URL)

    print("=== 1. Healthy baseline: primary provider (anthropic) serves the request ===")
    status, served_by, elapsed_ms = _send(client, headers, HEALTHY_MODEL)
    print(f"POST /v1/chat/completions model={HEALTHY_MODEL!r} -> {status}, served_by={served_by!r}, {elapsed_ms:.0f}ms")

    before = _breaker_snapshot(client)
    print(
        f"\nBreaker before fault injection: state={_STATE_NAMES[before['state']]} "
        f"(closed->open transitions so far: {before['opened']:.0f})"
    )

    print(
        f"\n=== 2. Injecting faults: {FAILURE_THRESHOLD + 2} requests to model={FAULT_MODEL!r} ===\n"
        "(mock-anthropic returns an instant 500 for this model; gateway retries\n"
        "the primary up to 3x with backoff, then falls back -- once "
        f"{FAILURE_THRESHOLD} failures land within the window, the breaker opens\n"
        "and later requests skip retrying the primary entirely)"
    )
    for i in range(FAILURE_THRESHOLD + 2):
        status, served_by, elapsed_ms = _send(client, headers, FAULT_MODEL)
        print(f"  request {i}: -> {status}, served_by={served_by!r}, {elapsed_ms:.0f}ms")

    after = _breaker_snapshot(client)
    print(
        f"\nBreaker after fault injection: state={_STATE_NAMES[after['state']]} "
        f"(closed->open transitions: {after['opened']:.0f}, "
        f"fallback activations anthropic->openai: {before['fallback_count']:.0f} -> {after['fallback_count']:.0f})"
    )
    assert after["state"] == 2.0, "expected breaker to be open after sustained faults"

    print("\n=== 3. One more request while breaker is open (should skip the primary, fast) ===")
    status, served_by, elapsed_ms = _send(client, headers, FAULT_MODEL)
    print(f"POST /v1/chat/completions model={FAULT_MODEL!r} -> {status}, served_by={served_by!r}, {elapsed_ms:.0f}ms")
    assert served_by == FALLBACK_MODEL, "expected the fallback provider (openai) to serve this, not anthropic"

    print(f"\n=== 4. Waiting {COOLDOWN_SECONDS}s cooldown, then probing with a healthy request ===")
    time.sleep(COOLDOWN_SECONDS)
    status, served_by, elapsed_ms = _send(client, headers, HEALTHY_MODEL)
    print(f"POST /v1/chat/completions model={HEALTHY_MODEL!r} -> {status}, served_by={served_by!r}, {elapsed_ms:.0f}ms")

    recovered = _breaker_snapshot(client)
    print(
        f"\nBreaker after recovery probe: state={_STATE_NAMES[recovered['state']]} "
        f"(open->half_open: {recovered['half_opened']:.0f}, half_open->closed: {recovered['closed']:.0f})"
    )
    assert recovered["state"] == 0.0, "expected breaker to be closed again after a successful probe"
    # served_by echoes the requested model name; since HEALTHY_MODEL only
    # succeeds on anthropic (fault-only on fallback's "--fault-error" entry
    # is irrelevant here), seeing it back proves anthropic -- not the
    # fallback -- served this request, i.e. the breaker is really closed.
    assert served_by == HEALTHY_MODEL, "expected the recovery probe to be served by anthropic again"

    client.close()
    print("\n=== Done: primary failed, breaker opened, fallback served traffic, breaker recovered. ===")


if __name__ == "__main__":
    main()
