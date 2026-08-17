from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from api.server.services import ApiServices
from compute.aws_connections import (
    AwsAccountPoolDrain,
    AwsAuthorizationCleanupResult,
)
from control.service import ControlPlaneService
from pydantic import JsonValue, TypeAdapter
from scheduler.containers import SchedulerContainerSubmitResult, SchedulerContainerSubmitStatus
from scheduler.service import Scheduler
from scheduler.state import SchedulerWorkerRequest
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPlan,
    AwsAccountConnection,
    AwsAccountValidationResult,
)
from shared.compute_fleet import ResourceStatus
from shared.deployment_records import DeploymentSpec
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import utc_now
from shared.usage import UsageMetric, UsageUnit

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _HttpResponse(Protocol):
    @property
    def content(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _WorkspaceEventAuthorizationPlanner:
    def plan(
        self,
        *,
        workspace_id: str,
        connection_id: str,
        generation: int,
        account_id: str,
        external_id: str,
        role_arn: str | None,
        active_authorization: AwsAccountAuthorizationGeneration | None,
        node_role_arn: str | None,
        node_instance_profile_arn: str | None,
    ) -> AwsAccountAuthorizationPlan:
        del workspace_id, connection_id, external_id, active_authorization
        return AwsAccountAuthorizationPlan(
            role_arn=role_arn or f"arn:aws:iam::{account_id}:role/control-g{generation}",
            authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
            node_role_arn=node_role_arn or f"arn:aws:iam::{account_id}:role/node",
            node_instance_profile_arn=node_instance_profile_arn
            or f"arn:aws:iam::{account_id}:instance-profile/node",
        )


@dataclass(frozen=True, slots=True)
class _UnusedWorkspaceEventConnectionValidator:
    def validate(
        self,
        connection: AwsAccountConnection,
        authorization: AwsAccountAuthorizationGeneration,
    ) -> AwsAccountValidationResult:
        del connection, authorization
        raise AssertionError("connection validation is outside this event publication test")


@dataclass(frozen=True, slots=True)
class _UnusedWorkspaceEventAuthorizationLifecycle:
    def reconcile_authorization_cleanup(
        self,
        *,
        account_id: str,
        external_id: str,
        authorization: AwsAccountAuthorizationGeneration,
        operation_id: str,
        node_role_arn: str | None,
        node_instance_profile_arn: str | None,
        remove_node_identity: bool,
    ) -> AwsAuthorizationCleanupResult:
        del (
            account_id,
            external_id,
            authorization,
            operation_id,
            node_role_arn,
            node_instance_profile_arn,
            remove_node_identity,
        )
        raise AssertionError("authorization cleanup is outside this event publication test")


@dataclass(frozen=True, slots=True)
class _UnusedWorkspaceEventPoolDrainer:
    def request_connection_drain(
        self,
        connection_id: str,
        *,
        workspace: str,
    ) -> AwsAccountPoolDrain:
        del connection_id, workspace
        raise AssertionError("pool draining is outside this event publication test")


class _LifecycleScheduler:
    def __init__(self, status: SchedulerContainerSubmitStatus) -> None:
        self.status = status

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        _ = ready_at
        return SchedulerContainerSubmitResult(
            status=self.status,
            container_id=request.container_id,
            reason="capacity unavailable"
            if self.status is SchedulerContainerSubmitStatus.Error
            else "",
        )


def test_hot_updates_do_not_publish_workspace_change_noise(
    isolated_services: ApiServices,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).get_workspace("default")
    repository = isolated_services.workspace_changes.repository

    task = isolated_services.tasks.create("noisy-save", workspace_id=workspace.id)
    worker = isolated_services.compute.register_worker()
    volume = isolated_services.volumes.get_or_create(
        "metered-volume", workspace=workspace.id, admit=None
    )
    cursor = repository.current_entry_id(workspace.id)

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

    assert (
        repository.read_after(
            workspace.id,
            cursor,
            block_milliseconds=0,
        )
        == ()
    )


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
    repository = isolated_services.workspace_changes.repository
    cursor = repository.current_entry_id(workspace.id)

    Scheduler(isolated_services).tick(now=due_at)

    updated = next(
        item
        for item in isolated_services.cron_jobs.list(workspace=workspace.id)
        if item.name == cron_job.name
    )
    assert updated.last_run_at == due_at
    assert updated.next_run_at is not None
    assert updated.next_run_at > due_at
    events = [
        record.event
        for record in repository.read_after(
            workspace.id,
            cursor,
            block_milliseconds=0,
        )
    ]
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
    repository = isolated_services.workspace_changes.repository
    cursor = repository.current_entry_id(workspace.id)

    assert service.acquire_concurrency(limit.id, workspace=workspace.id).acquired
    assert not service.acquire_concurrency(limit.id, workspace=workspace.id).acquired
    assert service.release_concurrency(limit.id, workspace=workspace.id).record.in_flight == 0
    assert service.release_concurrency(limit.id, workspace=workspace.id).record.in_flight == 0

    events = [
        record.event
        for record in repository.read_after(
            workspace.id,
            cursor,
            block_milliseconds=0,
            count=20,
        )
        if record.event.topic is WorkspaceChangeTopic.Concurrency
        and record.event.resource_id == limit.id
    ]
    assert [event.change for event in events] == [
        WorkspaceChangeType.Updated,
        WorkspaceChangeType.Updated,
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
