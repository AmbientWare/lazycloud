from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from database.repositories.execution import EventRepository, TaskRepository
from database.repositories.orchestration import ContainerRepository
from database.types import DatabaseSession
from pydantic import JsonValue
from shared.errors import UpstreamUnavailableError
from shared.events import Event, EventLevel
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import EventHistoryQuery
from shared.timestamps import utc_now
from shared.worker_events import TELEMETRY_EVENT_ACTIONS

from observability.context import ObservabilityContext
from observability.event_summary import (
    ContainerEventsBatchRequest,
    ContainerEventsBatchResponse,
    build_container_events_batch_response,
    normalize_batch_targets,
)
from observability.stream_state import RedisEventStreamRepository

EVENT_RETENTION = timedelta(days=30)
TELEMETRY_EVENT_RETENTION = timedelta(days=1)


@dataclass(slots=True)
class EventService:
    context: ObservabilityContext
    stream_events: RedisEventStreamRepository | None = None

    def emit(
        self,
        action: str,
        *,
        resource_type: str,
        resource_id: str,
        message: str,
        level: EventLevel = EventLevel.Info,
        data: dict[str, JsonValue] | None = None,
        workspace_id: str | None = None,
    ) -> Event:
        with self.context.database.session() as session:
            return EventRepository(session).records.create_across_workspaces(
                {
                    "action": action,
                    "level": level.value,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "message": message,
                    "data": data or {},
                },
                workspace_id=workspace_id,
            )

    def list(
        self,
        *,
        workspace_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        actions: Sequence[str] | None = None,
        container_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Event]:
        with self.context.database.session() as session:
            repository = EventRepository(session)
            related_task_ids = self._related_task_ids(session, container_id)
            if workspace_id is None:
                return repository.list_across_workspaces(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    actions=actions,
                    container_id=container_id,
                    related_task_ids=related_task_ids,
                    since=since,
                    until=until,
                    limit=limit,
                    offset=offset,
                )
            return repository.list(
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                actions=actions,
                container_id=container_id,
                related_task_ids=related_task_ids,
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )

    def count(
        self,
        *,
        workspace_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        actions: Sequence[str] | None = None,
        container_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        with self.context.database.session() as session:
            repository = EventRepository(session)
            related_task_ids = self._related_task_ids(session, container_id)
            if workspace_id is None:
                return repository.count_across_workspaces(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    actions=actions,
                    container_id=container_id,
                    related_task_ids=related_task_ids,
                    since=since,
                    until=until,
                )
            return repository.count(
                workspace_id=workspace_id,
                resource_type=resource_type,
                resource_id=resource_id,
                actions=actions,
                container_id=container_id,
                related_task_ids=related_task_ids,
                since=since,
                until=until,
            )

    def list_for_resource(
        self,
        *,
        resource_type: str,
        resource_id: str,
        workspace_id: str | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        with self.context.database.session() as session:
            repository = EventRepository(session)
            if workspace_id is None:
                return repository.list_across_workspaces(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    limit=limit,
                )
            return repository.list(
                resource_type=resource_type,
                resource_id=resource_id,
                workspace_id=workspace_id,
                limit=limit,
            )

    def prune(
        self,
        *,
        retention: timedelta = EVENT_RETENTION,
        telemetry_retention: timedelta = TELEMETRY_EVENT_RETENTION,
    ) -> int:
        """Delete audit events past `retention` and control-loop/request telemetry
        events past the much shorter `telemetry_retention`."""
        now = utc_now()
        with self.context.database.session() as session:
            repository = EventRepository(session)
            pruned = repository.prune(older_than=now - retention)
            pruned += repository.prune(
                older_than=now - telemetry_retention,
                actions=tuple(TELEMETRY_EVENT_ACTIONS),
            )
            return pruned

    def _related_task_ids(
        self,
        session: DatabaseSession,
        container_id: str | None,
    ) -> tuple[str, ...]:
        if not container_id:
            return ()
        return tuple(TaskRepository(session).ids_for_container(container_id))

    def container_events_batch(
        self,
        request: ContainerEventsBatchRequest,
        *,
        workspace_id: str | None = None,
    ) -> ContainerEventsBatchResponse:
        if self.stream_events is None:
            msg = "container event summaries require the event stream repository"
            raise UpstreamUnavailableError(msg)
        events: list[Event] = []
        for target in normalize_batch_targets(request):
            scope = self._resolve_container_scope(
                container_id=target.container_id,
                task_id=target.task_id,
                stub_id=target.stub_id,
                workspace_id=workspace_id,
            )
            if scope is None:
                continue
            records = self.stream_events.read_event_history(
                EventHistoryQuery(
                    workspace_id=scope.workspace_id,
                    stub_id=scope.stub_id,
                    container_id=scope.container_id,
                    event_types=(
                        EventRecordType.ContainerLifecycle,
                        EventRecordType.ContainerEvent,
                    ),
                )
            )
            events.extend(
                event
                for event in (_event_from_stream_record(record.body) for record in records)
                if event is not None
            )
        return build_container_events_batch_response(events, request)

    def _resolve_container_scope(
        self,
        *,
        container_id: str | None,
        task_id: str | None,
        stub_id: str | None,
        workspace_id: str | None,
    ) -> _ContainerScope | None:
        resolved_container_id = container_id or ""
        resolved_stub_id = stub_id or ""
        resolved_workspace_id = ""
        with self.context.database.session() as session:
            if not resolved_container_id and task_id:
                try:
                    task = TaskRepository(session).records.get_across_workspaces(task_id)
                except KeyError:
                    return None
                if task is None:
                    return None
                resolved_container_id = task.container_id or str(
                    task.kwargs.get("container_id") or ""
                )
                resolved_stub_id = resolved_stub_id or task.stub_id or ""
                resolved_workspace_id = task.workspace_id or ""
            if resolved_container_id:
                container = ContainerRepository(session).get_across_workspaces(
                    resolved_container_id
                )
                if container is not None:
                    resolved_stub_id = resolved_stub_id or container.stub_id or ""
                    resolved_workspace_id = container.workspace_id or resolved_workspace_id
        if not resolved_container_id:
            return None
        if workspace_id and resolved_workspace_id and resolved_workspace_id != workspace_id:
            return None
        return _ContainerScope(
            container_id=resolved_container_id,
            stub_id=resolved_stub_id,
            workspace_id=resolved_workspace_id or workspace_id or "",
        )


@dataclass(slots=True, frozen=True)
class _ContainerScope:
    container_id: str
    stub_id: str
    workspace_id: str


def _event_from_stream_record(body: Mapping[str, JsonValue]) -> Event | None:
    data = body.get("data")
    payload: dict[str, JsonValue] = (
        {str(key): value for key, value in data.items()} if isinstance(data, dict) else {}
    )
    event_type = str(body.get("type") or "")
    if not event_type:
        return None
    if "event_id" not in payload and isinstance(payload.get("id"), str):
        payload["event_id"] = payload["id"]
    container_id = str(payload.get("container_id") or body.get("containerid") or "")
    return Event(
        id=str(body.get("id") or ""),
        action=event_type,
        resource_type="container",
        resource_id=container_id,
        message=str(payload.get("message") or ""),
        data=payload,
        created_at=_stream_record_time(body, payload),
    )


def _stream_record_time(
    body: Mapping[str, JsonValue],
    payload: Mapping[str, JsonValue],
) -> datetime:
    for value in (payload.get("end_time"), payload.get("start_time"), body.get("time")):
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
        if isinstance(value, datetime):
            return value
    return datetime.now(UTC)
