from __future__ import annotations

from database.records.metrics import (
    MetricHistogramSample,
    MetricSample,
    MetricsLatest,
    metric_labels_key,
)
from database.repositories.observability import MetricRepository

from observability.context import ObservabilityContext


def _metric_suffix(labels: dict[str, str]) -> str:
    encoded = metric_labels_key(labels)
    return f"{{{encoded}}}" if encoded else ""


class MetricsService:
    """Latest platform metric state exposed through the Prometheus scrape endpoint."""

    def __init__(self, context: ObservabilityContext) -> None:
        self.context = context

    def increment(
        self,
        name: str,
        amount: float = 1,
        *,
        labels: dict[str, str] | None = None,
    ) -> MetricSample:
        with self.context.database.session() as session:
            repository = MetricRepository(session)
            sample = repository.increment_counter(name, amount, labels=labels)
        return sample

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> MetricSample:
        with self.context.database.session() as session:
            repository = MetricRepository(session)
            sample = repository.set_gauge(name, value, labels=labels)
        return sample

    def observe_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> MetricHistogramSample:
        with self.context.database.session() as session:
            repository = MetricRepository(session)
            sample = repository.observe_histogram(name, value, labels=labels)
        return sample

    def latest(self) -> MetricsLatest:
        with self.context.database.session() as session:
            return MetricRepository(session).latest()

    def prometheus_text(self) -> str:
        lines: list[str] = []
        latest = self.latest()
        for sample in [*latest.counters, *latest.gauges]:
            lines.append(f"{sample.name}{_metric_suffix(sample.labels)} {sample.value}")
        for histogram in latest.histograms:
            suffix = _metric_suffix(histogram.labels)
            lines.append(f"{histogram.name}_count{suffix} {histogram.count}")
            lines.append(f"{histogram.name}_sum{suffix} {histogram.total}")
            if histogram.minimum is not None:
                lines.append(f"{histogram.name}_min{suffix} {histogram.minimum}")
            if histogram.maximum is not None:
                lines.append(f"{histogram.name}_max{suffix} {histogram.maximum}")
            if histogram.last is not None:
                lines.append(f"{histogram.name}_last{suffix} {histogram.last}")
        return "\n".join(lines) + ("\n" if lines else "")
