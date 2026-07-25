from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field
from shared.contracts import ContractModel
from shared.timestamps import utc_now


class MetricKind(StrEnum):
    Counter = "counter"
    Gauge = "gauge"
    Histogram = "histogram"


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metric_labels_key(labels: dict[str, str] | None) -> str:
    """Canonical sorted `k="v",k2="v2"` encoding used for storage keys and exposition."""
    if not labels:
        return ""
    return ",".join(f'{key}="{escape_label_value(value)}"' for key, value in sorted(labels.items()))


class MetricSample(ContractModel):
    name: str
    value: float
    labels: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class MetricHistogramSample(ContractModel):
    name: str
    count: int = 0
    total: float = 0
    minimum: float | None = None
    maximum: float | None = None
    last: float | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class MetricsLatest(ContractModel):
    """Latest state of every metric: one entry per (kind, name, labels)."""

    counters: list[MetricSample] = Field(default_factory=list)
    gauges: list[MetricSample] = Field(default_factory=list)
    histograms: list[MetricHistogramSample] = Field(default_factory=list)
