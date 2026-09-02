from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import redis_text
from pydantic import JsonValue, TypeAdapter
from scheduler.service import Scheduler
from shared.compute_fleet import ResourceStatus
from shared.deployment_records import DeploymentSpec
from shared.http.workspace_changes import (
    WorkspaceChangeEvent,
    WorkspaceChangeTopic,
    WorkspaceChangeType,
)
from shared.timestamps import utc_now
from shared.usage import UsageMetric, UsageUnit

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _HttpResponse(Protocol):
    @property
    def content(self) -> bytes: ...


def test_hot_updates_do_not_publish_workspace_change_noise(
    isolated_services: ApiServices,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).get_workspace("default")

    task = isolated_services.tasks.create("noisy-save", workspace_id=workspace.id)
    worker = isolated_services.compute.register_worker()
    volume = isolated_services.volumes.get_or_create(
        "metered-volume", workspace=workspace.id, admit=None
    )
    cursor = _current_cursor(isolated_services, workspace.id)

    isolated_services.tasks.save(task)
    isolated_services.compute.set_worker_status(worker.id, ResourceStatus.Running)
    isolated_services.usage.record(
        workspace_id=workspace.id,
        resource_type="container",
        resource_id="sampled-container",
        metric=UsageMetric.CpuUsedCoreSeconds,
        quantity=1,
        unit=UsageUnit.Seconds,
    )
    isolated_services.usage.record(
        workspace_id=workspace.id,
        resource_type="container",
        resource_id="sampled-container",
        metric=UsageMetric.DiskReadBytes,
        quantity=1,
        unit=UsageUnit.Bytes,
    )
    isolated_services.volume_metering.reconcile_volume(
        volume.name,
        workspace_id=workspace.id,
        now=utc_now() + timedelta(seconds=61),
    )

    assert _changes_after(isolated_services, workspace.id, cursor) == []


def test_cron_execution_publishes_after_last_and_next_run_persist(
    isolated_services: ApiServices,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).get_workspace("default")
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(name="live-cron", handler="package:function", cron="every 1m"),
        workspace=workspace.id,
    )
    schedules = isolated_services.cron_jobs.list(workspace=workspace.id)
    assert len(schedules) == 1
    cron_job = schedules[0]
    assert cron_job.next_run_at is not None
    due_at = cron_job.next_run_at
    cursor = _current_cursor(isolated_services, workspace.id)

    Scheduler(isolated_services).tick(now=due_at)

    updated = next(
        item
        for item in isolated_services.cron_jobs.list(workspace=workspace.id)
        if item.name == cron_job.name
    )
    assert updated.last_run_at == due_at
    assert updated.next_run_at is not None
    assert updated.next_run_at > due_at
    events = _changes_after(isolated_services, workspace.id, cursor)
    assert any(
        event.topic is WorkspaceChangeTopic.Deployments
        and event.change.value == "updated"
        and event.resource_id == cron_job.name
        and event.deployment_id == deployment.id
        for event in events
    )


def test_concurrency_counter_publishes_only_committed_changes(
    isolated_services: ApiServices,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).get_workspace("default")
    service = ControlPlaneService(
        isolated_services.context,
        workspace_changes=isolated_services.workspace_changes,
    )
    limit = service.upsert_concurrency_limit(
        "live-counter",
        limit=1,
        workspace=workspace.id,
    )
    cursor = _current_cursor(isolated_services, workspace.id)

    assert service.acquire_concurrency(limit.id, workspace=workspace.id).acquired
    assert not service.acquire_concurrency(limit.id, workspace=workspace.id).acquired
    assert service.release_concurrency(limit.id, workspace=workspace.id).record.in_flight == 0
    assert service.release_concurrency(limit.id, workspace=workspace.id).record.in_flight == 0

    events = [
        event
        for event in _changes_after(isolated_services, workspace.id, cursor)
        if event.topic is WorkspaceChangeTopic.Concurrency and event.resource_id == limit.id
    ]
    assert [event.change for event in events] == [
        WorkspaceChangeType.Updated,
        WorkspaceChangeType.Updated,
    ]


def _current_cursor(services: ApiServices, workspace_id: str) -> str:
    key = services.workspace_changes.repository.stream_key(workspace_id)
    entries = services.redis().stream_reverse_range(key, count=1)
    return redis_text(entries[0][0]) if entries else "0-0"


def _changes_after(
    services: ApiServices,
    workspace_id: str,
    cursor: str,
) -> list[WorkspaceChangeEvent]:
    key = services.workspace_changes.repository.stream_key(workspace_id)
    return [
        WorkspaceChangeEvent.model_validate_json(redis_text(fields["event"]))
        for _key, entries in services.redis().stream_read({key: cursor}, count=100)
        for _entry_id, fields in entries
    ]


def _response_json(response: _HttpResponse) -> JsonValue:
    return _JSON_VALUE_ADAPTER.validate_json(response.content)


def _json_path(value: JsonValue, *path: str | int) -> JsonValue:
    current = value
    for segment in path:
        if isinstance(segment, str):
            assert isinstance(current, dict)
            current = current[segment]
        else:
            assert isinstance(current, list)
            current = current[segment]
    return current
