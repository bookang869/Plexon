"""OTel tracer/provider setup (ADR-013: traces exported to Grafana Tempo).
This is the only module that imports `opentelemetry.trace`/`opentelemetry.sdk`
directly for provider/tracer construction -- everywhere else in the gateway
calls `tracing.get_tracer()` instead, one clear module boundary per concern.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_DEFAULT_OTLP_ENDPOINT = "localhost:4317"


def configure_tracing(service_name: str = "plexon-gateway") -> None:
    """Sets a global TracerProvider with a BatchSpanProcessor exporting via
    OTLP gRPC to PLEXON_OTEL_EXPORTER_ENDPOINT (env var, default
    "localhost:4317" -- mirrors how PLEXON_REDIS_URL/PLEXON_DATABASE_URL are
    read in gateway/redis_client.py and gateway/db.py). Safe to call once at
    startup; never raises -- an unreachable/misconfigured Tempo must not
    prevent the gateway from starting or from serving requests (same
    fail-open reasoning already applied to the best-effort
    circuit_breaker_history/provider_health_history writes). On failure this
    simply leaves the default no-op tracer provider in place.
    """
    try:
        endpoint = os.environ.get("PLEXON_OTEL_EXPORTER_ENDPOINT", _DEFAULT_OTLP_ENDPOINT)
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception:
        logger.exception("failed to configure OTel tracing; continuing without span export")


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(__name__)
