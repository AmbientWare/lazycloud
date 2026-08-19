"""Platform metrics, counted in this process and exported on an interval.

Recording one costs an increment against an instrument held in memory. Nothing
reaches the database and nothing blocks the caller, which is what lets a hot
path be instrumented at all: the readiness probe runs twice a second per
container, and a row per observation priced that out.

The meter comes from the global provider `setup_telemetry` installs. Asking for
one before that runs is safe. The API hands back a proxy that resolves when a
provider appears, so instrument creation does not have to wait for process
startup to have reached telemetry. With no provider ever installed, every
instrument is a no-op and nothing is exported.
"""

from __future__ import annotations

import threading

from opentelemetry import metrics

# `create_gauge` is public, but the type it returns is exported as `_Gauge`
# while the synchronous gauge is still marked unstable. Importing it under that
# name keeps the registry typed. Dropping the annotation instead would hide the
# one instrument whose semantics differ from the others.
from opentelemetry.metrics import Counter, Histogram, _Gauge

INSTRUMENTATION_NAME = "lazycloud.platform"


class MetricsService:
    def __init__(self, meter_name: str = INSTRUMENTATION_NAME) -> None:
        self._meter = metrics.get_meter(meter_name)
        self._lock = threading.Lock()
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._histograms: dict[str, Histogram] = {}

    def increment(
        self,
        name: str,
        amount: float = 1,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._counter(name).add(amount, attributes=labels or {})

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._gauge(name).set(value, attributes=labels or {})

    def observe_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._histogram(name).record(value, attributes=labels or {})

    def _counter(self, name: str) -> Counter:
        with self._lock:
            instrument = self._counters.get(name)
            if instrument is None:
                instrument = self._meter.create_counter(name)
                self._counters[name] = instrument
            return instrument

    def _gauge(self, name: str) -> _Gauge:
        with self._lock:
            instrument = self._gauges.get(name)
            if instrument is None:
                instrument = self._meter.create_gauge(name)
                self._gauges[name] = instrument
            return instrument

    def _histogram(self, name: str) -> Histogram:
        with self._lock:
            instrument = self._histograms.get(name)
            if instrument is None:
                instrument = self._meter.create_histogram(name)
                self._histograms[name] = instrument
            return instrument


__all__ = ["INSTRUMENTATION_NAME", "MetricsService"]
