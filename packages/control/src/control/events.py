from __future__ import annotations

from typing import Protocol

from database.records.apps import StubRecord
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue
from shared.events import Event, EventLevel
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType


class ControlEventEmitter(Protocol):
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
    ) -> Event: ...


def publish_workload_change(
    publisher: WorkspaceChangePublisher | None,
    stub: StubRecord,
    change: WorkspaceChangeType,
) -> None:
    if publisher is None:
        return
    publisher.emit_change(
        workspace_id=stub.workspace_id,
        topic=WorkspaceChangeTopic.Workloads,
        change=change,
        resource_id=stub.id,
        app_id=stub.app_id,
        deployment_id=stub.deployment_id,
        stub_id=stub.id,
    )
