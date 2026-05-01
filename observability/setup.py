"""Telemetry bootstrap — call setup_telemetry() once at process startup.

Reads standard OTel env vars — same convention as the Ekam service stack:
  OTEL_EXPORTER_OTLP_ENDPOINT  e.g. http://<ekam-host>:4317
  OTEL_EXPORTER_OTLP_PROTOCOL  grpc (default) or http/protobuf
  OTEL_RESOURCE_ATTRIBUTES     service.name=agno-hive,deployment.environment=dev
  OTEL_SDK_DISABLED            true to disable entirely

No-op if OTEL_SDK_DISABLED=true or OTEL_EXPORTER_OTLP_ENDPOINT is unset.
"""
import os

_initialized = False


def setup_telemetry(service_name: str = "agno-hive") -> None:
    global _initialized
    if _initialized:
        return

    if os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true":
        return

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return  # Observability disabled — set OTEL_EXPORTER_OTLP_ENDPOINT to enable

    protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").strip()

    from opentelemetry import trace, metrics
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    resource = Resource.create({SERVICE_NAME: service_name})

    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        # gRPC exporters read OTEL_EXPORTER_OTLP_ENDPOINT automatically
        span_exporter = OTLPSpanExporter()
        metric_exporter = OTLPMetricExporter()
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        span_exporter = OTLPSpanExporter()
        metric_exporter = OTLPMetricExporter()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(metric_exporter, export_interval_millis=60_000)
        ],
    )
    metrics.set_meter_provider(meter_provider)

    _initialized = True
    print(f"[observability] telemetry enabled → {endpoint} ({protocol})")
