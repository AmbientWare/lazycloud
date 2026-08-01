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
        """Render the exposition format a scraper can actually parse.

        The aggregate observations here are summaries, not histograms — there
        are no buckets — so they are typed and suffixed as summaries, and the
        min/max/last extrema are their own gauges rather than suffixes no
        parser recognises.
        """
        lines: list[str] = []
        latest = self.latest()
        typed: set[str] = set()

        def declare(name: str, kind: str) -> None:
            if name not in typed:
                typed.add(name)
                lines.append(f"# TYPE {name} {kind}")

        for sample in sorted(latest.counters, key=lambda item: item.name):
            declare(sample.name, "counter")
            lines.append(f"{sample.name}{_metric_suffix(sample.labels)} {sample.value}")
        for sample in sorted(latest.gauges, key=lambda item: item.name):
            declare(sample.name, "gauge")
            lines.append(f"{sample.name}{_metric_suffix(sample.labels)} {sample.value}")
        for histogram in sorted(latest.histograms, key=lambda item: item.name):
            suffix = _metric_suffix(histogram.labels)
            declare(histogram.name, "summary")
            lines.append(f"{histogram.name}_count{suffix} {histogram.count}")
            lines.append(f"{histogram.name}_sum{suffix} {histogram.total}")
            for extremum, value in (
                ("min", histogram.minimum),
                ("max", histogram.maximum),
                ("last", histogram.last),
            ):
                if value is not None:
                    declare(f"{histogram.name}_{extremum}", "gauge")
                    lines.append(f"{histogram.name}_{extremum}{suffix} {value}")
        return "\n".join(lines) + ("\n" if lines else "")
