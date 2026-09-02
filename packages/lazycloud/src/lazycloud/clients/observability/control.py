from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import quote, urlencode

from pydantic import JsonValue
from shared.http.observability import (
    EventHistoryRequest,
    EventQueryResponse,
    LogQueryRequest,
    LogQueryResponse,
    LogRecord,
)
from shared.http.usage import UsageCostGroupKey, UsageCostListResponse, UsageRecordListResponse
from shared.http_transport import HttpChannel
from shared.transport_retry import TRANSIENT_TRANSPORT_ERRORS, TransientRetry
from shared.usage import UsageMetric

from lazycloud.control import workspace_query
from lazycloud.json_contracts import parse_json_value, validate_json_object


class ObservabilityControlChannel(Protocol):
    def get(self, path: str) -> JsonValue: ...

    def stream_get(self, path: str) -> Iterator[str]: ...


class ObservabilityClient(Protocol):
    def logs(self, request: LogQueryRequest | None = None) -> LogQueryResponse: ...

    def events(self, request: EventHistoryRequest | None = None) -> EventQueryResponse: ...

    def usage_records(
        self,
        *,
        metric: UsageMetric | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> UsageRecordListResponse: ...

    def usage_costs(
        self,
        *,
        start: datetime,
        end: datetime,
        group_by: UsageCostGroupKey = UsageCostGroupKey.App,
        app_id: str | None = None,
        workload_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> UsageCostListResponse: ...

    def stream_logs(
        self,
        request: LogQueryRequest | None = None,
        *,
        max_events: int = 0,
        cursor: str | None = None,
        seq_num: int | None = None,
        wait: int | None = None,
        wait_seconds: float = 1.0,
        clamp: bool | None = None,
    ) -> Iterator[LogRecord]: ...

    def stream_events(
        self,
        request: EventHistoryRequest | None = None,
        *,
        max_events: int = 0,
        cursor: str | None = None,
        clamp: bool | None = None,
    ) -> JsonValue: ...


@dataclass
class ObservabilityControlClient:
    channel: ObservabilityControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> ObservabilityControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def logs(self, request: LogQueryRequest | None = None) -> LogQueryResponse:
        selected = request or LogQueryRequest(workspace_id=self.workspace)
        return LogQueryResponse.model_validate(self.channel.get(_logs_path(selected)))

    def events(self, request: EventHistoryRequest | None = None) -> EventQueryResponse:
        selected = request or EventHistoryRequest(workspace_id=self.workspace)
        return EventQueryResponse.model_validate(self.channel.get(_events_path(selected)))

    def usage_records(
        self,
        *,
        metric: UsageMetric | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> UsageRecordListResponse:
        query: dict[str, str | int] = {**workspace_query(self.workspace), "limit": limit}
        if metric is not None:
            query["metric"] = metric.value
        if resource_type is not None:
            query["resource_type"] = resource_type
        if resource_id is not None:
            query["resource_id"] = resource_id
        if start is not None:
            query["start"] = start.isoformat()
        if end is not None:
            query["end"] = end.isoformat()
        if cursor:
            query["cursor"] = cursor
        return UsageRecordListResponse.model_validate(
            self.channel.get(f"/api/v1/usage/records?{urlencode(query)}")
        )

    def usage_costs(
        self,
        *,
        start: datetime,
        end: datetime,
        group_by: UsageCostGroupKey = UsageCostGroupKey.App,
        app_id: str | None = None,
        workload_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> UsageCostListResponse:
        """What usage cost, read from the priced ledger rather than recomputed.

        The same rows the payment provider is metered from, broken to the
        component each rate is published per — so what a container was charged
        for a processor is answerable without holding the rate card or knowing
        which of its windows measured anything.
        """

        query: dict[str, str | int] = {
            "workspace": self.workspace,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "group_by": group_by.value,
            "limit": limit,
        }
        if app_id is not None:
            query["app_id"] = app_id
        if workload_id is not None:
            query["workload_id"] = workload_id
        if cursor:
            query["cursor"] = cursor
        return UsageCostListResponse.model_validate(
            self.channel.get(f"/api/v1/usage/costs?{urlencode(query)}")
        )

    def stream_logs(
        self,
        request: LogQueryRequest | None = None,
        *,
        max_events: int = 0,
        cursor: str | None = None,
        seq_num: int | None = None,
        wait: int | None = None,
        wait_seconds: float = 1.0,
        clamp: bool | None = None,
    ) -> Iterator[LogRecord]:
        selected = request or LogQueryRequest(workspace_id=self.workspace)
        if cursor is not None or seq_num is not None or wait is not None or clamp is not None:
            updates: dict[str, str | int | bool] = {}
            if cursor is not None:
                updates["cursor"] = cursor
            if seq_num is not None:
                updates["seq_num"] = seq_num
            if wait is not None:
                updates["wait"] = wait
            if clamp is not None:
                updates["clamp"] = clamp
            selected = selected.model_copy(update=updates)
        retry = TransientRetry()
        emitted = 0
        last_cursor: str | None = None
        while True:
            remaining = max_events - emitted if max_events > 0 else 0
            if max_events > 0 and remaining <= 0:
                return
            try:
                for record in _sse_log_records(
                    self.channel.stream_get(
                        _logs_stream_path(
                            selected,
                            max_events=remaining,
                            wait_seconds=wait_seconds,
                        )
                    )
                ):
                    retry.reset()
                    emitted += 1
                    if record.cursor:
                        last_cursor = record.cursor
                    yield record
                    if max_events > 0 and emitted >= max_events:
                        return
            except TimeoutError:
                # Idle long-poll read timeout: the control plane is reachable
                # but has no new records; reconnect without consuming the
                # transient-failure budget.
                retry.reset()
            except TRANSIENT_TRANSPORT_ERRORS as exc:
                retry.backoff(exc)
            else:
                if selected.wait is not None or last_cursor is None:
                    # Bounded-wait streams end when the wait window closes, and
                    # without a resume position a reconnect could only replay.
                    return
                # A follow stream ending cleanly mid-follow means the control
                # plane went away; resume within the bounded retry budget.
                retry.backoff(ConnectionError("log stream closed by control plane"))
            if last_cursor is not None:
                selected = selected.model_copy(
                    update={"cursor": last_cursor, "seq_num": None, "clamp": True}
                )

    def stream_events(
        self,
        request: EventHistoryRequest | None = None,
        *,
        max_events: int = 0,
        cursor: str | None = None,
        clamp: bool | None = None,
    ) -> JsonValue:
        selected = request or EventHistoryRequest(workspace_id=self.workspace)
        return self.channel.get(
            _events_stream_path(
                selected,
                max_events=max_events,
                cursor=cursor,
                clamp=clamp,
            )
        )


def _logs_path(request: LogQueryRequest) -> str:
    return _with_query(
        "/api/v1/logs",
        {
            **workspace_query(request.workspace_id),
            **request.model_dump(
                mode="json",
                exclude={"workspace_id"},
                exclude_none=True,
            ),
        },
    )


def _logs_stream_path(
    request: LogQueryRequest,
    *,
    max_events: int,
    wait_seconds: float,
) -> str:
    return _with_query(
        "/api/v1/logs/stream",
        _logs_stream_params(
            request,
            max_events=max_events,
            wait_seconds=wait_seconds,
        ),
    )


def _logs_stream_params(
    request: LogQueryRequest,
    *,
    max_events: int,
    wait_seconds: float,
) -> dict[str, JsonValue]:
    params = validate_json_object(
        {
            **workspace_query(request.workspace_id),
            **request.model_dump(
                mode="json",
                exclude={"workspace_id"},
                exclude_none=True,
            ),
            "follow": True,
            "max_events": max_events,
        }
    )
    if "wait" not in params:
        params["wait_seconds"] = wait_seconds
    return params


def _events_path(request: EventHistoryRequest) -> str:
    return _with_query(
        "/api/v1/events/history",
        request.model_dump(
            mode="json",
            exclude_none=True,
        ),
    )


def _events_stream_path(
    request: EventHistoryRequest,
    *,
    max_events: int,
    cursor: str | None,
    clamp: bool | None,
) -> str:
    base = "/api/v1/events/stream"
    if request.task_id:
        base = f"/api/v1/events/tasks/{quote(request.task_id, safe='')}/stream"
    elif request.container_id:
        base = f"/api/v1/events/containers/{quote(request.container_id, safe='')}/stream"
    elif request.resource_type == "app" and request.resource_id:
        base = f"/api/v1/events/apps/{quote(request.resource_id, safe='')}/stream"
    elif request.resource_type == "stub" and request.resource_id:
        base = f"/api/v1/events/stubs/{quote(request.resource_id, safe='')}/stream"
    params: dict[str, JsonValue] = {
        "follow": True,
        "max_events": max_events,
    }
    if cursor is not None:
        params["cursor"] = cursor
    if clamp is not None:
        params["clamp"] = clamp
    return _with_query(
        base,
        params,
    )


def _with_query(path: str, params: Mapping[str, JsonValue]) -> str:
    if not params:
        return path
    return f"{path}?{urlencode(params)}"


def _sse_log_records(lines: Iterator[str]) -> Iterator[LogRecord]:
    for event, payload in _sse_json_events(lines):
        if event != "log" or not isinstance(payload, dict):
            continue
        try:
            yield LogRecord.model_validate(payload)
        except ValueError:
            continue


def _sse_json_events(lines: Iterator[str]) -> Iterator[tuple[str, JsonValue]]:
    event = ""
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            if data_lines:
                yield event, parse_json_value("\n".join(data_lines))
            event = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)


__all__ = [
    "ObservabilityClient",
    "ObservabilityControlChannel",
    "ObservabilityControlClient",
]
