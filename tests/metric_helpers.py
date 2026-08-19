"""Read what the process recorded through its meter.

The OpenTelemetry API allows one meter provider per process and ignores every
later attempt to install another, so the whole test session shares one reader.
Readings are therefore cumulative across tests, which is safe here because every
assertion filters on labels carrying a workspace and stub id, and those differ
per test.

Collection drains. A cumulative counter reports its total on every collection,
but a synchronous gauge reports only when its value changed since the last one,
so a second read would find the counter and lose the gauge. Each collection
folds into a running latest-value-per-series map, which is the shape the
assertions want and the shape the database-backed helper used to return.
"""

from __future__ import annotations

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

_READER = InMemoryMetricReader()
_LATEST: dict[tuple[str, frozenset[tuple[str, str]]], float] = {}
_SEEN_WITHOUT_VALUE: set[str] = set()


def install_metric_reader() -> None:
    """Give this process the provider the assertions read from.

    Called once, before anything records. An instrument built earlier still
    reports through it, because the API hands out a proxy that resolves when a
    provider appears, but a value recorded before this runs went to a no-op and
    is gone.
    """

    metrics.set_meter_provider(MeterProvider(metric_readers=[_READER]))


def _collect() -> None:
    data = _READER.get_metrics_data()
    for resource_metric in getattr(data, "resource_metrics", ()) or ():
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                for point in metric.data.data_points:
                    value = getattr(point, "value", None)
                    if value is None:
                        _SEEN_WITHOUT_VALUE.add(metric.name)
                        continue
                    attributes = frozenset(
                        (str(key), str(item)) for key, item in (point.attributes or {}).items()
                    )
                    _LATEST[(metric.name, attributes)] = float(value)


def metric_value(name: str, **labels: str) -> float:
    """The latest value of one series, found by name and a subset of its labels."""

    _collect()
    for (recorded_name, attributes), value in _LATEST.items():
        if recorded_name != name:
            continue
        recorded = dict(attributes)
        if all(recorded.get(key) == item for key, item in labels.items()):
            return value
    if name in _SEEN_WITHOUT_VALUE:
        msg = f"metric {name!r} is a histogram; this helper reads single-valued points only"
        raise AssertionError(msg)
    msg = f"metric {name!r} with labels {labels!r} not found"
    raise AssertionError(msg)
