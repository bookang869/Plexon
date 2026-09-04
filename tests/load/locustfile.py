"""Locust load-test scenario (PRD Core Feature 5, TRD §10): 5,000+ concurrent
requests across mixed team keys/models/priorities against a live gateway
(`docker compose -f deploy/docker-compose.yml up -d`, host http://localhost:8000).

Team pool: read from scripts/demo_teams.json (step 0's setup script writes this
on the host; run `uv run python3 scripts/setup_demo_teams.py` before this file
-- Locust runs on the host, outside the Compose network, and needs the
host-visible copy of that file, not the one the `setup` Compose service writes
inside its own container).

`ollama` has no live Docker Compose service yet (ADR-017) -- this scenario
deliberately never requests `llama3` directly as a primary model (it would
just generate transport-error noise unrelated to what's being load-tested);
it remains configured as the tail of `fast_tier`'s fallback chain but is never
reached in practice (anthropic and openai are both live).

Run: `uv run locust -f tests/load/locustfile.py --host http://localhost:8000
-u 5000 -r 100 --run-time 3m --headless --csv=tests/load/results`
(5,000 concurrent users, ramping 100/s, 3-minute steady state, per TRD §10's
"5,000+ concurrent requests" target -- for a quick smoke run during
development, use much smaller -u/-r/--run-time values instead).

Gateway-overhead latency (target <10ms, TRD §10): Locust's own response-time
percentiles measure end-to-end latency (gateway + mocked-provider round
trip), not gateway overhead in isolation -- there's no separate
gateway-only timer exposed today (gateway_latency_seconds, from the
observability phase, measures the same end-to-end call). To estimate
overhead, this file also runs a small-weight task hitting mock-openai
directly, bypassing the gateway entirely -- comparing that task's median
latency against the normal chat-completion task's median in the Locust
report approximates gateway overhead. This is an approximation, not an
exact isolated measurement; note that plainly in the results, don't
present it as a precise <10ms figure.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from locust import HttpUser, between, task

_DEMO_TEAMS_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "demo_teams.json"

_FAULT_MODEL = "claude-sonnet--fault-error"
_FAULT_TEAM_ID = "demo-realtime-highvolume"

# mock-openai's own published host-port (deploy/docker-compose.yml), used as
# an absolute URL below -- Locust's runner overwrites every User class'
# `host` attribute with whatever --host was passed on the command line
# (locust/runners.py's spawn path does this unconditionally), so a
# class-level `host` override on DirectMockUser wouldn't survive `--host
# http://localhost:8000`. Passing an absolute URL to self.client.post()
# sidesteps that -- requests' URL-joining leaves an absolute URL untouched
# regardless of the session's base_url.
_MOCK_OPENAI_URL = "http://localhost:8081/v1/chat/completions"

try:
    _TEAMS: list[dict] = json.loads(_DEMO_TEAMS_PATH.read_text())
except FileNotFoundError as exc:
    raise RuntimeError(
        f"{_DEMO_TEAMS_PATH} not found -- run `uv run python3 scripts/setup_demo_teams.py` "
        "on the host before starting Locust"
    ) from exc

_FAULT_TEAM = next(t for t in _TEAMS if t["team_id"] == _FAULT_TEAM_ID)


def _random_team() -> dict:
    return random.choice(_TEAMS)


def _random_priority_headers() -> dict[str, str]:
    return {"X-Priority": "batch"} if random.random() < 0.3 else {}


def _chat_payload(model: str, tag: str) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": f"load test {tag}"}],
        "max_tokens": 64,
    }


class GatewayUser(HttpUser):
    """Normal team traffic: chat completions across each team's own
    allowed_models (excluding the magic fault-injection model -- that's
    driven by a separate low-weight task below), mixed X-Priority tiers.
    """

    weight = 1
    wait_time = between(0.5, 2.0)

    @task(10)
    def chat_completion(self) -> None:
        team = _random_team()
        model = random.choice(team["allowed_models"])
        headers = {
            "Authorization": f"Bearer {team['api_key']}",
            **_random_priority_headers(),
        }
        self.client.post(
            "/v1/chat/completions",
            json=_chat_payload(model, "realtime"),
            headers=headers,
            name="/v1/chat/completions",
        )

    @task(3)
    def chat_completion_batch_priority(self) -> None:
        team = _random_team()
        model = random.choice(team["allowed_models"])
        headers = {
            "Authorization": f"Bearer {team['api_key']}",
            "X-Priority": "batch",
        }
        self.client.post(
            "/v1/chat/completions",
            json=_chat_payload(model, "batch"),
            headers=headers,
            name="/v1/chat/completions",
        )

    @task(1)
    def simulated_outage_request(self) -> None:
        """Requests claude-sonnet--fault-error against demo-realtime-highvolume
        (the only team whose allowed_models includes it) -- every real
        attempt against anthropic fails and falls back to gpt-4o-mini,
        continuously exercising the fallback path (and, at sustained volume,
        the circuit breaker) throughout the run, simulating a provider
        partially down for the whole test rather than a scripted time-boxed
        outage window.
        """
        headers = {
            "Authorization": f"Bearer {_FAULT_TEAM['api_key']}",
            **_random_priority_headers(),
        }
        self.client.post(
            "/v1/chat/completions",
            json=_chat_payload(_FAULT_MODEL, "outage"),
            headers=headers,
            name="/v1/chat/completions [fault->fallback]",
        )


class DirectMockUser(HttpUser):
    """Low-weight baseline: hits mock-openai directly (bypassing the gateway)
    to establish a provider-latency floor for the gateway-overhead estimate
    described above.
    """

    weight = 1
    wait_time = between(0.5, 2.0)

    @task
    def direct_mock_call(self) -> None:
        self.client.post(
            _MOCK_OPENAI_URL,
            json=_chat_payload("gpt-4o-mini", "direct-baseline"),
            name="/v1/chat/completions [direct-mock-baseline]",
        )
