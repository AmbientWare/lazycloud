from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from types import TracebackType
from typing import Self
from urllib.parse import urlparse, urlunparse

from pydantic import Field, field_validator, model_validator
from shared.app_identity import CONTROL_PLANE_SERVICE_NAME
from shared.contracts import ContractModel

DEFAULT_OTLP_HTTP_ENDPOINT = "http://localhost:4318"
DEFAULT_TRACE_EXPORT_PATH = "/v1/traces"
DEFAULT_METER_INTERVAL_SECONDS = 60.0
DEFAULT_TRACE_INTERVAL_SECONDS = 5.0
DEFAULT_EXPORT_TIMEOUT_SECONDS = 10.0


class TelemetryExporterKind(StrEnum):
    OtlpHttpTrace = "otlp-http-trace"
    ConsoleMetric = "console-metric"
    ConsoleLog = "console-log"


class TelemetryPropagator(StrEnum):
    TraceContext = "tracecontext"
    Baggage = "baggage"


class TelemetryTransportSecurity(StrEnum):
    Disabled = "disabled"
    InsecureHttp = "insecure-http"
    Https = "https"


class TelemetryProviderKind(StrEnum):
    Tracer = "tracer"
    Meter = "meter"
    Logger = "logger"


class TelemetrySpanStatus(StrEnum):
    Ok = "ok"
    Error = "error"


class TelemetryConfig(ContractModel):
    enabled: bool = False
    service_name: str = CONTROL_PLANE_SERVICE_NAME
    endpoint: str = DEFAULT_OTLP_HTTP_ENDPOINT
    meter_interval_seconds: float = DEFAULT_METER_INTERVAL_SECONDS
    trace_interval_seconds: float = DEFAULT_TRACE_INTERVAL_SECONDS
    trace_sample_ratio: float = 1.0
    export_timeout_seconds: float = DEFAULT_EXPORT_TIMEOUT_SECONDS
    export_traces: bool = True
    export_metrics: bool = True
    export_logs: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    resource_attributes: dict[str, str] = Field(default_factory=dict)

    @field_validator("service_name")
    @classmethod
    def service_name_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "telemetry service name cannot be blank"
            raise ValueError(msg)
        return stripped

    @field_validator("meter_interval_seconds", "trace_interval_seconds", "export_timeout_seconds")
    @classmethod
    def positive_seconds(cls, value: float) -> float:
        if value <= 0:
            msg = "telemetry intervals and timeouts must be positive"
            raise ValueError(msg)
        return value

    @field_validator("trace_sample_ratio")
    @classmethod
    def clamp_sample_ratio(cls, value: float) -> float:
        if value < 0:
            return 0.0
        if value > 1:
            return 1.0
        return value

    @model_validator(mode="after")
    def require_endpoint_when_trace_export_enabled(self) -> Self:
        if self.enabled and self.export_traces and not self.endpoint.strip():
            msg = "telemetry endpoint is required when trace export is enabled"
            raise ValueError(msg)
        return self


class TelemetryEndpointPlan(ContractModel):
    raw_endpoint: str
    trace_export_url: str
    security: TelemetryTransportSecurity
    host: str
    port: int | None = None


class TelemetrySetupPlan(ContractModel):
    enabled: bool
    service_name: str
    resource_attributes: dict[str, str] = Field(default_factory=dict)
    propagators: tuple[TelemetryPropagator, ...] = (
        TelemetryPropagator.TraceContext,
        TelemetryPropagator.Baggage,
    )
    exporters: tuple[TelemetryExporterKind, ...] = ()
    endpoint: TelemetryEndpointPlan | None = None
    trace_sample_ratio: float = 1.0
    meter_interval_millis: int = int(DEFAULT_METER_INTERVAL_SECONDS * 1000)
    trace_batch_delay_millis: int = int(DEFAULT_TRACE_INTERVAL_SECONDS * 1000)
    export_timeout_millis: int = int(DEFAULT_EXPORT_TIMEOUT_SECONDS * 1000)
    shutdown_order: tuple[TelemetryProviderKind, ...] = ()


class TelemetrySpanPlan(ContractModel):
    tracer_name: str
    span_name: str
    attributes: dict[str, str] = Field(default_factory=dict)
    status: TelemetrySpanStatus = TelemetrySpanStatus.Ok


class TelemetryHandle:
    def __init__(
        self,
        plan: TelemetrySetupPlan,
        shutdown_callbacks: list[Callable[[], None]] | None = None,
    ) -> None:
        self.plan = plan
        self._shutdown_callbacks = shutdown_callbacks or []
        self.closed = False

    def shutdown(self) -> None:
        if self.closed:
            return
        errors: list[BaseException] = []
        for callback in self._shutdown_callbacks:
            try:
                callback()
            except BaseException as exc:  # pragma: no cover - defensive cleanup path
                errors.append(exc)
        self.closed = True
        if errors:
            raise BaseExceptionGroup("telemetry shutdown failed", errors)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.shutdown()


def build_telemetry_plan(config: TelemetryConfig) -> TelemetrySetupPlan:
    resource_attributes = {"service.name": config.service_name} | config.resource_attributes
    exporters: list[TelemetryExporterKind] = []
    if config.export_traces:
        exporters.append(TelemetryExporterKind.OtlpHttpTrace)
    if config.export_metrics:
        exporters.append(TelemetryExporterKind.ConsoleMetric)
    if config.export_logs:
        exporters.append(TelemetryExporterKind.ConsoleLog)

    if not config.enabled:
        return TelemetrySetupPlan(
            enabled=False,
            service_name=config.service_name,
            resource_attributes=resource_attributes,
            exporters=tuple(exporters),
            trace_sample_ratio=config.trace_sample_ratio,
            meter_interval_millis=_seconds_to_millis(config.meter_interval_seconds),
            trace_batch_delay_millis=_seconds_to_millis(config.trace_interval_seconds),
            export_timeout_millis=_seconds_to_millis(config.export_timeout_seconds),
            shutdown_order=(),
        )

    shutdown_order: list[TelemetryProviderKind] = []
    if config.export_logs:
        shutdown_order.append(TelemetryProviderKind.Logger)
    if config.export_metrics:
        shutdown_order.append(TelemetryProviderKind.Meter)
    if config.export_traces:
        shutdown_order.append(TelemetryProviderKind.Tracer)

    return TelemetrySetupPlan(
        enabled=True,
        service_name=config.service_name,
        resource_attributes=resource_attributes,
        exporters=tuple(exporters),
        endpoint=plan_telemetry_endpoint(config.endpoint) if config.export_traces else None,
        trace_sample_ratio=config.trace_sample_ratio,
        meter_interval_millis=_seconds_to_millis(config.meter_interval_seconds),
        trace_batch_delay_millis=_seconds_to_millis(config.trace_interval_seconds),
        export_timeout_millis=_seconds_to_millis(config.export_timeout_seconds),
        shutdown_order=tuple(shutdown_order),
    )


def plan_telemetry_endpoint(endpoint: str) -> TelemetryEndpointPlan:
    parsed = urlparse(endpoint.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = "telemetry endpoint must be an http or https URL"
        raise ValueError(msg)

    path = parsed.path.rstrip("/") or DEFAULT_TRACE_EXPORT_PATH
    if path == "/" or path == "":
        path = DEFAULT_TRACE_EXPORT_PATH
    elif not path.endswith(DEFAULT_TRACE_EXPORT_PATH):
        path = f"{path}{DEFAULT_TRACE_EXPORT_PATH}"

    trace_url = urlunparse(parsed._replace(path=path, params="", query="", fragment=""))
    security = (
        TelemetryTransportSecurity.Https
        if parsed.scheme == "https"
        else TelemetryTransportSecurity.InsecureHttp
    )
    return TelemetryEndpointPlan(
        raw_endpoint=endpoint,
        trace_export_url=trace_url,
        security=security,
        host=parsed.hostname or "",
        port=parsed.port,
    )


def setup_telemetry(
    config: TelemetryConfig,
    *,
    install_global: bool = True,
) -> TelemetryHandle:
    plan = build_telemetry_plan(config)
    if not plan.enabled:
        return TelemetryHandle(plan)

    from opentelemetry import _logs, metrics, propagate, trace
    from opentelemetry.baggage.propagation import W3CBaggagePropagator
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    resource = Resource.create(plan.resource_attributes)
    callbacks: list[Callable[[], None]] = []

    if install_global:
        propagate.set_global_textmap(
            CompositePropagator(
                [
                    TraceContextTextMapPropagator(),
                    W3CBaggagePropagator(),
                ]
            )
        )

    if config.export_traces:
        if plan.endpoint is None:
            msg = "trace endpoint plan is required when trace export is enabled"
            raise RuntimeError(msg)
        trace_provider = TracerProvider(
            resource=resource,
            sampler=TraceIdRatioBased(plan.trace_sample_ratio),
        )
        span_exporter = OTLPSpanExporter(
            endpoint=plan.endpoint.trace_export_url,
            headers=config.headers or None,
            timeout=config.export_timeout_seconds,
        )
        trace_provider.add_span_processor(
            BatchSpanProcessor(
                span_exporter,
                schedule_delay_millis=plan.trace_batch_delay_millis,
                export_timeout_millis=plan.export_timeout_millis,
            )
        )
        if install_global:
            trace.set_tracer_provider(trace_provider)
        callbacks.append(trace_provider.shutdown)

    if config.export_metrics:
        metric_reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(),
            export_interval_millis=plan.meter_interval_millis,
            export_timeout_millis=plan.export_timeout_millis,
        )
        meter_provider = MeterProvider(metric_readers=[metric_reader], resource=resource)
        if install_global:
            metrics.set_meter_provider(meter_provider)
        callbacks.append(meter_provider.shutdown)

    if config.export_logs:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(ConsoleLogExporter()))
        if install_global:
            _logs.set_logger_provider(logger_provider)
        callbacks.append(logger_provider.shutdown)

    return TelemetryHandle(plan, callbacks)


@contextmanager
def trace_span(
    tracer_name: str,
    span_name: str,
    attributes: Mapping[str, str | bool | int | float] | None = None,
) -> Iterator[TelemetrySpanPlan]:
    from opentelemetry import trace

    normalized = {key: str(value) for key, value in (attributes or {}).items()}
    with trace.get_tracer(tracer_name).start_as_current_span(span_name) as span:
        for key, value in normalized.items():
            span.set_attribute(key, value)
        status = TelemetrySpanStatus.Ok
        try:
            yield TelemetrySpanPlan(
                tracer_name=tracer_name,
                span_name=span_name,
                attributes=normalized,
                status=status,
            )
        except BaseException:
            status = TelemetrySpanStatus.Error
            span.set_attribute("error", True)
            raise


def _seconds_to_millis(value: float) -> int:
    return max(1, int(value * 1000))
