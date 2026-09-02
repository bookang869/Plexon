"""Tests for gateway/observability/tracing.py -- OTel span wiring across the
non-streaming request pipeline (TRD §8). Uses a test-local TracerProvider +
InMemorySpanExporter (ADR-006: no real Tempo, no network calls), monkeypatched
in place of gateway.observability.tracing.get_tracer -- every span opened by
gateway code (auth/team_auth.py, routes.py, main.py's root-span middleware,
all of which call tracing.get_tracer()) lands in an in-memory buffer this test
can inspect directly. Runs against the real app (in-process) and the real
mock-openai/mock-anthropic containers, same as tests/test_routing.py.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gateway.config.loader import start_config_watcher
from gateway.main import app
from gateway.observability import tracing
from gateway.providers.errors import RetryableProviderError

_config_loaded = False


@pytest_asyncio.fixture
async def client(db_pool, redis_client):
    global _config_loaded
    if not _config_loaded:
        start_config_watcher()
        _config_loaded = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as ac:
        yield ac


@pytest.fixture
def span_exporter(monkeypatch):
    """A TracerProvider built directly in the test (not via
    configure_tracing(), which reads the real PLEXON_OTEL_EXPORTER_ENDPOINT
    env var), using SimpleSpanProcessor so spans land in the in-memory buffer
    synchronously as soon as each `with ... start_as_current_span(...)` block
    exits -- no batching/flush delay to race against in a test.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    test_tracer = provider.get_tracer(__name__)
    monkeypatch.setattr(tracing, "get_tracer", lambda: test_tracer)
    yield exporter
    exporter.clear()


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _spans_by_name(exporter: InMemorySpanExporter) -> dict:
    return {s.name: s for s in exporter.get_finished_spans()}


_ALL_SPAN_NAMES = {
    "request.receipt",
    "auth",
    "rate_limit_check",
    "provider_selection",
    "provider_call",
    "response_processing",
    "response_delivery",
}


@pytest.mark.asyncio
async def test_successful_request_produces_all_seven_spans_as_children_of_root(
    client, seeded_team, span_exporter
):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 200
    body = resp.json()

    spans = _spans_by_name(span_exporter)
    assert _ALL_SPAN_NAMES <= spans.keys()

    root = spans["request.receipt"]
    for name in _ALL_SPAN_NAMES - {"request.receipt"}:
        child = spans[name]
        assert child.parent is not None
        assert child.parent.span_id == root.context.span_id

    assert spans["auth"].attributes["team_id"] == seeded_team["team_id"]

    assert spans["rate_limit_check"].attributes["team_id"] == seeded_team["team_id"]
    assert spans["rate_limit_check"].attributes["model_requested"] == "claude-sonnet"

    assert spans["provider_selection"].attributes["team_id"] == seeded_team["team_id"]
    assert spans["provider_selection"].attributes["model_requested"] == "claude-sonnet"

    assert spans["provider_call"].attributes["team_id"] == seeded_team["team_id"]
    assert spans["provider_call"].attributes["model_requested"] == "claude-sonnet"
    assert spans["provider_call"].attributes["model_served"] == body["model"]

    assert spans["response_processing"].attributes["input_tokens"] == body["usage"]["prompt_tokens"]
    assert spans["response_processing"].attributes["output_tokens"] == body["usage"]["completion_tokens"]
    assert "cost_usd" in spans["response_processing"].attributes
    assert spans["response_processing"].attributes["cost_usd"] > 0


@pytest.mark.asyncio
async def test_disallowed_model_still_produces_early_spans_without_crashing(
    client, seeded_team, span_exporter
):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "not-an-allowed-model", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 403

    spans = _spans_by_name(span_exporter)
    assert {"request.receipt", "auth", "rate_limit_check"} <= spans.keys()
    assert "provider_selection" not in spans
    assert "provider_call" not in spans
    assert "response_processing" not in spans


@pytest.mark.asyncio
async def test_fallback_provider_sets_differing_model_served(client, seeded_team, span_exporter):
    """Same stub-adapter technique as test_routing.py's
    test_primary_failure_falls_back_to_next_provider_in_chain -- forces the
    real orchestration path (call_with_resilience) to exhaust "anthropic"
    and fall back to "openai" per fast_tier in tests/fixtures/test_config.yaml.
    """
    from gateway.providers import registry as provider_registry

    class _AlwaysFailsAdapter:
        async def chat_completion(self, request):
            raise RetryableProviderError("stub: primary always fails")

    provider_registry._adapters["anthropic"] = _AlwaysFailsAdapter()

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth_headers(seeded_team["api_key"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "gpt-4o-mini"

    spans = _spans_by_name(span_exporter)
    provider_call = spans["provider_call"]
    assert provider_call.attributes["model_requested"] == "claude-sonnet"
    assert provider_call.attributes["model_served"] == "gpt-4o-mini"
    assert provider_call.attributes["model_requested"] != provider_call.attributes["model_served"]
