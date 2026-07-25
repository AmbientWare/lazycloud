from __future__ import annotations

from collections.abc import Sequence

from database.records.metrics import (
    MetricHistogramSample,
    MetricSample,
)


def metric_value(
    samples: Sequence[MetricSample | MetricHistogramSample],
    name: str,
    **labels: str,
) -> float:
    for sample in samples:
        if sample.name != name:
            continue
        if all(sample.labels.get(key) == value for key, value in labels.items()):
            return sample.value if isinstance(sample, MetricSample) else sample.last or 0
    msg = f"metric {name!r} with labels {labels!r} not found"
    raise AssertionError(msg)
