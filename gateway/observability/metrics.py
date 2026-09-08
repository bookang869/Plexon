"""Prometheus metric definitions (TRD §8, ADR-026: `prometheus-client` with a
direct `/metrics` scrape endpoint, not an OTel metrics exporter). Metric
objects are module-level constants; call sites elsewhere in the gateway
import them and call `.labels(...).inc()`/`.observe()`/`.set()` directly --
no wrapper/helper layer, since each call site's available labels differ.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

gateway_requests_total = Counter(
    "gateway_requests_total", "Total gateway requests", ["team", "model", "provider"]
)

gateway_errors_total = Counter(
    "gateway_errors_total",
    "Total gateway request errors",
    ["team", "model", "provider", "error_type"],
)

gateway_latency_seconds = Histogram(
    "gateway_latency_seconds", "Provider call latency in seconds", ["provider"]
)

gateway_tokens_total = Counter(
    "gateway_tokens_total", "Total tokens processed", ["team", "direction"]
)

gateway_cost_usd_total = Counter(
    "gateway_cost_usd_total", "Total cost in USD", ["team"]
)

gateway_fallback_triggered_total = Counter(
    "gateway_fallback_triggered_total",
    "Total fallback activations",
    ["from_provider", "to_provider"],
)

gateway_circuit_breaker_state = Gauge(
    "gateway_circuit_breaker_state",
    "Current circuit breaker state (0=closed, 1=half_open, 2=open)",
    ["provider"],
)

gateway_circuit_breaker_transitions_total = Counter(
    "gateway_circuit_breaker_transitions_total",
    "Total circuit breaker state transitions",
    ["provider", "from_state", "to_state"],
)

gateway_overhead_seconds = Histogram(
    "gateway_overhead_seconds",
    "Gateway-only processing time (total request time minus the provider-call span), in seconds",
    ["route"],
)
