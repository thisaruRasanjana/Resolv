"""
src/tracing.py — OpenTelemetry configuration.

Configures OTLP exporter to send traces to a collector if available.
If no collector is available, traces are silently dropped.
"""

import os
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Initialize TracerProvider
resource = Resource.create({"service.name": "resolv"})
provider = TracerProvider(resource=resource)

# Add OTLP Exporter if configured (e.g. OTLP endpoint)
# Usually set via OTEL_EXPORTER_OTLP_ENDPOINT
otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
if otlp_endpoint:
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
    provider.add_span_processor(processor)

trace.set_tracer_provider(provider)

# Global tracer instance to use across the app
tracer = trace.get_tracer(__name__)
