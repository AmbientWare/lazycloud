from __future__ import annotations

from api.server.services import ApiServices
from database.tables.observability import MetricTable
from observability.metrics import MetricsService
from sqlalchemy import func, select


def _metric_row_count(services: ApiServices) -> int:
    with services.context.database.session() as session:
        return int(session.scalar(select(func.count()).select_from(MetricTable)) or 0)


def test_metric_writes_keep_single_latest_row(isolated_services: ApiServices) -> None:
    metrics = MetricsService(isolated_services.context)

    metrics.increment("tasks_total", labels={"status": "complete"})
    counter = metrics.increment("tasks_total", labels={"status": "complete"})
    assert counter.value == 2

    metrics.set_gauge("workers", 3)
    gauge = metrics.set_gauge("workers", 5)
    assert gauge.value == 5

    metrics.observe_histogram("request_ms", 10)
    histogram = metrics.observe_histogram("request_ms", 30)
    assert histogram.count == 2
    assert histogram.total == 40
    assert histogram.minimum == 10
    assert histogram.maximum == 30
    assert histogram.last == 30

    latest = metrics.latest()
    assert [sample.value for sample in latest.counters] == [2]
    assert [sample.value for sample in latest.gauges] == [5]
    assert [sample.count for sample in latest.histograms] == [2]

    # One latest-state row per metric; no snapshot/history blobs accumulate.
    assert _metric_row_count(isolated_services) == 3

    text = metrics.prometheus_text()
    assert 'tasks_total{status="complete"} 2.0' in text
    assert "request_ms_count 2" in text
