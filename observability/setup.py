"""Telemetry bootstrap — call setup_telemetry() once at process startup.

If OTLP_ENDPOINT is not configured this is a complete no-op.
Backend-agnostic: works with SigNoz, Jaeger, Tempo, or any OTLP-compatible backend.
"""
_initialized = False


def setup_telemetry(service_name: str = "agno-hive") -> None:
    global _initialized
    if _initialized:
        return

    from config.config import config

    if not config.otlp_endpoint:
        return  # Observability disabled — set OTLP_ENDPOINT to enable

    from opentelemetry import trace, metrics
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    endpoint = config.otlp_endpoint.rstrip("/")
    resource = Resource.create({"service.name": service_name})

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(tracer_provider)

    # Metrics (exported every 60s)
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
                export_interval_millis=60_000,
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)

    _initialized = True
    print(f"[observability] telemetry enabled → {endpoint}")
