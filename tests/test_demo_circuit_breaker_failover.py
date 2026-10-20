"""Tests for the pure helpers in scripts/demo_circuit_breaker_failover.py.

The script's `main()` is an end-to-end demo against a live docker-compose
stack (gateway + mock-anthropic + mock-openai) and isn't unit-testable --
same reasoning as benchmarks/bench_failover.py, which is only exercised via
`pytest -m benchmark` against the real stack. These tests cover the two
pieces of logic that don't need the stack: parsing a Prometheus metric line
and loading a team's API key from scripts/demo_teams.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.demo_circuit_breaker_failover import _load_api_key, _metric

_SAMPLE_METRICS_TEXT = """\
gateway_fallback_triggered_total{from_provider="anthropic",to_provider="openai"} 110.0
gateway_circuit_breaker_state{provider="anthropic"} 2.0
gateway_circuit_breaker_transitions_total{from_state="closed",provider="anthropic",to_state="open"} 6.0
"""


def test_metric_parses_matching_line():
    assert _metric(_SAMPLE_METRICS_TEXT, "gateway_circuit_breaker_state", 'provider="anthropic"') == 2.0
    assert (
        _metric(
            _SAMPLE_METRICS_TEXT,
            "gateway_fallback_triggered_total",
            'from_provider="anthropic",to_provider="openai"',
        )
        == 110.0
    )


def test_metric_returns_zero_when_absent():
    assert _metric(_SAMPLE_METRICS_TEXT, "gateway_circuit_breaker_state", 'provider="openai"') == 0.0
    assert _metric("", "gateway_circuit_breaker_state", 'provider="anthropic"') == 0.0


def test_load_api_key_reads_seeded_team(tmp_path):
    demo_file = tmp_path / "demo_teams.json"
    demo_file.write_text(
        json.dumps(
            [
                {"team_id": "demo-realtime-highvolume", "api_key": "test-key-123"},
                {"team_id": "demo-other", "api_key": "other-key"},
            ]
        )
    )

    assert _load_api_key("demo-realtime-highvolume", demo_file) == "test-key-123"


def test_load_api_key_raises_when_team_missing(tmp_path):
    demo_file = tmp_path / "demo_teams.json"
    demo_file.write_text(json.dumps([{"team_id": "demo-other", "api_key": "x"}]))

    with pytest.raises(SystemExit):
        _load_api_key("demo-realtime-highvolume", demo_file)
