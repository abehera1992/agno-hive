"""Metric instruments — import these anywhere to record measurements."""
from opentelemetry import metrics

_meter = metrics.get_meter("agno-hive")

task_duration = _meter.create_histogram(
    name="agno.task.duration",
    description="Duration of task runs in seconds",
    unit="s",
)

task_counter = _meter.create_counter(
    name="agno.task.count",
    description="Total number of tasks completed",
)
