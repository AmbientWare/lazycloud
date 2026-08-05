from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from gateway.http import (
    AgentEventRecord,
    AgentLogRecord,
    AgentMetricSnapshot,
    AgentTelemetryRequest,
    AgentTelemetryResponse,
)

DEFAULT_AGENT_TELEMETRY_BATCH_SIZE = 128
DEFAULT_AGENT_TELEMETRY_BUFFER_SIZE = 1024


class AgentTelemetrySource(StrEnum):
    Agent = "agent"
    RouteProxy = "route-proxy"
    Worker = "worker"


class AgentTelemetryStream(StrEnum):
    Stdout = "stdout"
    Stderr = "stderr"
    System = "system"


class AgentTelemetryEventType(StrEnum):
    Agent = "agent"
    Route = "agent.route"
    Transport = "agent.transport"


class AgentTelemetryClient(Protocol):
    def stream_agent_telemetry(
        self,
        request: AgentTelemetryRequest,
    ) -> AgentTelemetryResponse: ...


@dataclass(slots=True)
class AgentTelemetryBuffer:
    max_records: int = DEFAULT_AGENT_TELEMETRY_BUFFER_SIZE
    batch_size: int = DEFAULT_AGENT_TELEMETRY_BATCH_SIZE
    _logs: list[AgentLogRecord] = field(default_factory=list, init=False)
    _events: list[AgentEventRecord] = field(default_factory=list, init=False)
    _metrics: AgentMetricSnapshot | None = field(default=None, init=False)
    _dropped_records: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        if self.max_records <= 0:
            msg = "agent telemetry buffer size must be positive"
            raise ValueError(msg)
        if self.batch_size <= 0:
            msg = "agent telemetry batch size must be positive"
            raise ValueError(msg)

    @property
    def dropped_records(self) -> int:
        with self._lock:
            return self._dropped_records

    def size(self) -> int:
        with self._lock:
            return _telemetry_record_count(self._logs, self._events, self._metrics)

    def enqueue_log(
        self,
        line: str,
        *,
        source: AgentTelemetrySource | str = AgentTelemetrySource.Agent,
        stream: AgentTelemetryStream | str = AgentTelemetryStream.Stdout,
        worker_id: str = "",
        level: str = "info",
        timestamp_unix_nano: int = 0,
    ) -> bool:
        text = line.rstrip("\r")
        if not text:
            return True
        return self.enqueue_request(
            AgentTelemetryRequest(
                agent_token="",
                logs=[
                    AgentLogRecord(
                        source=str(source),
                        worker_id=worker_id,
                        level=level,
                        stream=str(stream),
                        line=text,
                        timestamp_unix_nano=timestamp_unix_nano or time.time_ns(),
                    )
                ],
            )
        )

    def enqueue_event(
        self,
        *,
        event_type: AgentTelemetryEventType | str,
        action: str,
        status: str = "",
        message: str = "",
        attrs: dict[str, str] | None = None,
        timestamp_unix_nano: int = 0,
    ) -> bool:
        if not action:
            return True
        return self.enqueue_request(
            AgentTelemetryRequest(
                agent_token="",
                events=[
                    AgentEventRecord(
                        event_type=str(event_type),
                        action=action,
                        status=status,
                        message=message,
                        attrs=attrs or {},
                        timestamp_unix_nano=timestamp_unix_nano or time.time_ns(),
                    )
                ],
            )
        )

    def set_metrics(self, metrics: AgentMetricSnapshot | None) -> bool:
        if metrics is None:
            return True
        return self.enqueue_request(AgentTelemetryRequest(agent_token="", metrics=metrics))

    def enqueue_request(self, request: AgentTelemetryRequest | None) -> bool:
        if request is None:
            return True
        size = _agent_telemetry_request_size(request)
        with self._lock:
            current_size = _telemetry_record_count(self._logs, self._events, self._metrics)
            if current_size + size > self.max_records:
                self._dropped_records += size
                return False
            self._logs.extend(request.logs)
            self._events.extend(request.events)
            if request.metrics is not None:
                self._metrics = request.metrics
        return True

    def drain_batches(self, agent_token: str) -> list[AgentTelemetryRequest]:
        with self._lock:
            logs = self._logs
            events = self._events
            metrics = self._metrics
            self._logs = []
            self._events = []
            self._metrics = None
        return _build_telemetry_batches(
            agent_token,
            logs=logs,
            events=events,
            metrics=metrics,
            batch_size=self.batch_size,
        )

    def flush(self, agent_token: str, client: AgentTelemetryClient) -> int:
        sent = 0
        for request in self.drain_batches(agent_token):
            response = client.stream_agent_telemetry(request)
            if not response.ok:
                msg = response.err_msg or "agent telemetry rejected"
                raise RuntimeError(msg)
            sent += 1
        return sent


@dataclass(slots=True)
class AgentTelemetryLineWriter:
    telemetry: AgentTelemetryBuffer
    source: AgentTelemetrySource | str = AgentTelemetrySource.Agent
    stream: AgentTelemetryStream | str = AgentTelemetryStream.Stdout
    worker_id: str = ""
    level: str = "info"
    _buffer: str = field(default="", init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def write(self, data: str | bytes) -> int:
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self.telemetry.enqueue_log(
                    line,
                    source=self.source,
                    stream=self.stream,
                    worker_id=self.worker_id,
                    level=self.level,
                )
        return len(data)

    def close(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            line = self._buffer
            self._buffer = ""
        self.telemetry.enqueue_log(
            line,
            source=self.source,
            stream=self.stream,
            worker_id=self.worker_id,
            level=self.level,
        )


def _build_telemetry_batches(
    agent_token: str,
    *,
    logs: list[AgentLogRecord],
    events: list[AgentEventRecord],
    metrics: AgentMetricSnapshot | None,
    batch_size: int,
) -> list[AgentTelemetryRequest]:
    batches: list[AgentTelemetryRequest] = []
    pending_logs = list(logs)
    pending_events = list(events)
    pending_metrics = metrics
    while pending_logs or pending_events or pending_metrics is not None:
        request_logs = pending_logs[:batch_size]
        del pending_logs[: len(request_logs)]
        remaining = max(batch_size - len(request_logs), 0)
        request_events = pending_events[:remaining]
        del pending_events[: len(request_events)]
        request_metrics = None
        if pending_metrics is not None and (
            len(request_logs) + len(request_events) < batch_size
            or (not request_logs and not request_events)
        ):
            request_metrics = pending_metrics
            pending_metrics = None
        batches.append(
            AgentTelemetryRequest(
                agent_token=agent_token,
                logs=request_logs,
                events=request_events,
                metrics=request_metrics,
            )
        )
    return batches


def _telemetry_record_count(
    logs: list[AgentLogRecord],
    events: list[AgentEventRecord],
    metrics: AgentMetricSnapshot | None,
) -> int:
    return len(logs) + len(events) + (1 if metrics is not None else 0)


def _agent_telemetry_request_size(request: AgentTelemetryRequest) -> int:
    size = _telemetry_record_count(request.logs, request.events, request.metrics)
    return size or 1
