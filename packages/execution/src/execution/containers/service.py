from __future__ import annotations

import base64
import binascii
import hashlib
import json
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from coordination.event_bus import EventBusEvent, EventBusEventType, EventBusSendResult
from database.repositories.images import ImageArchiveRepository
from database.repositories.orchestration import (
    ContainerPageCursor,
    ContainerRepository,
)
from database.types import DatabaseSession
from foundation.ids import optional_uuid
from observability.events import EventService
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import Field
from shared.compute_policy import ComputePlacementTarget
from shared.container_requests import (
    OciRuntimeName,
    StopContainerReason,
    WorkerContainerRequestPayload,
    WorkerStartupKind,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.errors import InvalidInputError, NotFoundError
from shared.events import EventLevel
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.scheduling import (
    SchedulerContainerCancellationResult,
    SchedulerContainerSubmitResult,
    SchedulerWorkerRequest,
    gpu_count_for_capacity,
)
from shared.tasks import TaskStatus
from shared.timestamps import utc_now

from execution.containers.planning import (
    DEFAULT_CONTAINER_DISK_MIB,
    ContainerSchedulingOptions,
    resolve_oci_runtime,
    validate_checkpoint_request,
)
from execution.context import ExecutionContext
from execution.tasks import TaskService


def _shell_split(command: str | Iterable[str]) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command)
    return [str(part) for part in command]


class ContainerScheduler(Protocol):
    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult: ...


class SchedulerContainerCancellation(Protocol):
    def cancel(self, container_id: str) -> SchedulerContainerCancellationResult: ...


class ContainerEventBus(Protocol):
    def send(self, event: EventBusEvent) -> EventBusSendResult: ...


class AppExecutionAdmission(Protocol):
    def assert_active(
        self,
        session: DatabaseSession,
        *,
        app_id: str,
        workspace_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ContainerPageResult:
    data: list[ContainerRecord]
    next: str = ""


class ContainerCursorPayload(ContractModel):
    created_at: datetime
    id: UUID
    scope: str


class PendingContainerReservation(ContractModel):
    id: str | None = None
    name: str
    image: str
    command: list[str]
    workspace_id: str
    stub_id: str | None = None
    app_id: str | None = None
    task_id: str | None = None
    machine_id: str | None = None
    worker_id: str | None = None
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    ports: dict[str, int] = Field(default_factory=dict)
    network_blocked: bool = False
    network_allow_list: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class ContainerService:
    context: ExecutionContext
    events: EventService
    tasks: TaskService
    app_admission: AppExecutionAdmission
    scheduler: ContainerScheduler
    scheduler_cancellation: SchedulerContainerCancellation
    event_bus: ContainerEventBus
    workspace_changes: WorkspaceChangePublisher

    def reserve_pending(
        self,
        session: DatabaseSession,
        reservation: PendingContainerReservation,
    ) -> ContainerRecord:
        app_id = optional_uuid(reservation.app_id, field="app_id")
        if app_id is not None:
            self.app_admission.assert_active(
                session,
                app_id=app_id,
                workspace_id=reservation.workspace_id,
            )
        values = reservation.model_dump(mode="python", exclude_none=True)
        values["app_id"] = app_id
        values["stub_id"] = optional_uuid(reservation.stub_id, field="stub_id")
        values["task_id"] = optional_uuid(reservation.task_id, field="task_id")
        values["machine_id"] = optional_uuid(reservation.machine_id, field="machine_id")
        values["worker_id"] = optional_uuid(reservation.worker_id, field="worker_id")
        values["status"] = ContainerStatus.Pending.value
        return ContainerRepository(session).records.create(
            values,
            workspace_id=reservation.workspace_id,
            name=reservation.name,
            status=ContainerStatus.Pending.value,
        )

    def run(
        self,
        name: str,
        image: str,
        command: str | Iterable[str],
        *,
        workspace_id: str | None = None,
        stub_id: str | None = None,
        app_id: str | None = None,
        machine_id: str | None = None,
        worker_id: str | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        ports: dict[str, int] | None = None,
        timeout_seconds: int | None = None,
        cpu_millicores: int = 0,
        memory_mib: int = 0,
        disk_mib: int = DEFAULT_CONTAINER_DISK_MIB,
        gpu_type: str = "",
        gpu_request: Iterable[str] | None = None,
        gpu_count: int = 0,
        pool_selector: str = "",
        requested_placement: ComputePlacementTarget | None = None,
        runtime: OciRuntimeName | str = OciRuntimeName.Runc,
        runtime_class: str = "",
        docker_enabled: bool = False,
        block_network: bool = False,
        allow_list: Iterable[str] | None = None,
        preemptible: bool = False,
        gpu_limit: int = 0,
        cpu_limit_millicores: int = 0,
    ) -> ContainerRecord:
        _ = timeout_seconds
        argv = _shell_split(command)
        with self.context.database.session() as session:
            workspace = (
                self.context.workspace(session, workspace_id)
                if workspace_id is not None
                else self.context.workspace(session)
            )
            resolved_app_id = optional_uuid(app_id, field="app_id")
            task = self.tasks.create_in_transaction(
                session,
                f"container:{name}",
                workspace_id=workspace.id,
                app_id=resolved_app_id,
                stub_id=stub_id,
                command=argv,
            )
            record = self.reserve_pending(
                session,
                PendingContainerReservation(
                    name=name,
                    image=image,
                    command=argv,
                    workspace_id=workspace.id,
                    stub_id=stub_id,
                    app_id=resolved_app_id,
                    machine_id=machine_id,
                    worker_id=worker_id,
                    task_id=task.id,
                    cwd=str(cwd) if cwd is not None else None,
                    env=env or {},
                    ports=ports or {},
                    network_blocked=block_network,
                    network_allow_list=[str(item) for item in allow_list or []],
                ),
            )
        self.tasks.publish_created(task)
        self.publish_lifecycle_change(record, WorkspaceChangeType.Created)

        submitted = self.submit_scheduler_request(
            record,
            ContainerSchedulingOptions(
                workspace_name=workspace.name,
                stub_type="container",
                startup_kind=WorkerStartupKind.PodRun,
                entrypoint=argv,
                cwd=str(cwd) if cwd is not None else "/workspace",
                env=env,
                ports=list(ports.values()) if ports else None,
                requested_ports=list(ports.values()) if ports else None,
                cpu_millicores=cpu_millicores,
                memory_mib=memory_mib,
                disk_mib=disk_mib,
                gpu_type=gpu_type,
                gpu_request=list(gpu_request) if gpu_request is not None else None,
                gpu_count=gpu_count,
                pool_selector=pool_selector,
                requested_placement=requested_placement,
                runtime=runtime,
                runtime_class=runtime_class,
                docker_enabled=docker_enabled,
                block_network=block_network,
                allow_list=list(allow_list) if allow_list is not None else None,
                preemptible=preemptible,
                gpu_limit=gpu_limit,
                cpu_limit_millicores=cpu_limit_millicores,
            ),
        )
        if not submitted.accepted:
            task = self.tasks.transition(task, TaskStatus.Failed, error=submitted.reason)
            record.status = ContainerStatus.Failed
            record.exit_code = 1
            record.finished_at = utc_now()
            with self.context.database.session() as session:
                failed = ContainerRepository(session).records.upsert(
                    record,
                    workspace_id=record.workspace_id,
                    name=record.name,
                    status=record.status.value,
                )
            self.events.emit(
                "container.schedule.failed",
                resource_type="container",
                resource_id=record.id,
                message=submitted.reason or f"failed to schedule container {record.name}",
                level=EventLevel.Error,
                data={"task_id": task.id},
                workspace_id=record.workspace_id,
            )
            self.publish_lifecycle_change(failed, WorkspaceChangeType.Updated)
            return failed

        self.events.emit(
            "container.scheduled",
            resource_type="container",
            resource_id=record.id,
            message=f"scheduled container {record.name}",
            level=EventLevel.Info,
            data={"task_id": task.id, "scheduler_status": submitted.status.value},
            workspace_id=record.workspace_id,
        )
        return record

    def submit_scheduler_request(
        self,
        record: ContainerRecord,
        options: ContainerSchedulingOptions,
    ) -> SchedulerContainerSubmitResult:
        runtime_name, runtime_constraint = resolve_oci_runtime(
            runtime=options.runtime,
            runtime_class=options.runtime_class,
            docker_enabled=options.docker_enabled,
        )
        validate_checkpoint_request(
            startup_kind=options.startup_kind,
            enabled=options.checkpoint_enabled,
            restoring=bool(options.checkpoint_id),
            runtime=runtime_name,
            gpu_count=max(
                options.gpu_count,
                gpu_count_for_capacity(
                    options.gpu_type,
                    list(options.gpu_request or ()),
                    options.gpu_count,
                ),
            ),
            readiness_path=options.checkpoint_readiness_path,
            readiness_port=options.checkpoint_readiness_port,
        )
        image_id = options.image_id if options.image_id is not None else record.image
        payload = WorkerContainerRequestPayload(
            image_id=image_id,
            archive_sha256=self._authorized_archive_sha256(
                image_id,
                workspace_id=record.workspace_id,
            ),
            app_id=options.app_id if options.app_id is not None else record.app_id or "",
            deployment_id=options.deployment_id or "",
            stub_type=options.stub_type,
            workspace_name=options.workspace_name,
            entrypoint=[str(item) for item in (options.entrypoint or record.command)],
            cwd=str(options.cwd or record.cwd or "/workspace"),
            env=(
                list(options.env_list)
                if options.env_list is not None
                else _env_mapping_to_list(options.env or record.env)
            ),
            ports=_valid_ports(options.ports),
            requested_ports=_valid_ports(options.requested_ports),
            checkpoint_exposed_ports=_valid_ports(options.checkpoint_exposed_ports),
            checkpoint_id=options.checkpoint_id,
            checkpoint_enabled=options.checkpoint_enabled,
            checkpoint_readiness_path=options.checkpoint_readiness_path,
            checkpoint_readiness_port=options.checkpoint_readiness_port,
            checkpoint_readiness_timeout_seconds=options.checkpoint_readiness_timeout_seconds,
            checkpoint_readiness_interval_seconds=options.checkpoint_readiness_interval_seconds,
            startup_kind=options.startup_kind,
            runtime=runtime_name,
            docker_enabled=options.docker_enabled,
            block_network=options.block_network,
            allow_list=[str(item) for item in options.allow_list or []],
            cost_per_ms=options.cost_per_ms,
            mounts=list(options.mounts or []),
            secret_names=[str(name) for name in options.secret_names or []],
            gateway_token_required=options.gateway_token_required,
            workspace_storage_required=options.workspace_storage_required,
            workspace_storage_available=options.workspace_storage_available,
            workspace_storage_base_mount_path=options.workspace_storage_base_mount_path,
            # Disk is a per-container ceiling rather than scheduled capacity, so it
            # travels in the worker payload and not on the scheduler request.
            disk_limit_bytes=options.disk_mib * 1024 * 1024,
        )
        request = SchedulerWorkerRequest(
            workspace_id=record.workspace_id,
            stub_id=record.stub_id or options.stub_type or "containers",
            deployment_id=options.deployment_id or "",
            container_id=record.id,
            cpu_millicores=options.cpu_millicores,
            memory_mib=options.memory_mib,
            gpu_type=options.gpu_type,
            gpu_request=[str(item) for item in options.gpu_request or []],
            gpu_count=options.gpu_count,
            pool_selector=options.pool_selector,
            requested_placement=options.requested_placement,
            runtime_class=runtime_constraint,
            docker_enabled=options.docker_enabled,
            preemptible=options.preemptible,
            gpu_limit=options.gpu_limit,
            cpu_limit_millicores=options.cpu_limit_millicores,
            payload=payload.model_dump(mode="json"),
        )
        return self.scheduler.submit(request, ready_at=options.ready_at)

    def _authorized_archive_sha256(self, image_id: str, *, workspace_id: str) -> str:
        """Archive digest for this image, resolved through the workspace's own authorization.

        Every stub type funnels its dispatch through `submit_scheduler_request`, so
        resolving the digest here is what lets a worker treat it as an
        authorization-checked cache key: a workspace that cannot resolve the archive
        dispatches no digest, and the worker falls back to asking the broker.
        """
        if not image_id:
            return ""
        with self.context.database.session() as session:
            archive = ImageArchiveRepository(session).get_authorized(
                image_id,
                workspace_id=workspace_id,
            )
        return archive.sha256 if archive is not None else ""

    def get(self, container_id: str) -> ContainerRecord:
        """System-authority lookup for the execution engine's own control flow."""
        with self.context.database.session() as session:
            record = ContainerRepository(session).get_across_workspaces(container_id)
        if record is None:
            msg = f"container not found: {container_id}"
            raise NotFoundError(msg)
        return record

    def unsettled_preemptions(self, *, limit: int) -> list[ContainerRecord]:
        with self.context.database.session() as session:
            return ContainerRepository(session).unsettled_preemptions_across_workspaces(limit=limit)

    def mark_preemption_settled(self, container_id: str) -> None:
        with self.context.database.session() as session:
            ContainerRepository(session).mark_preemption_settled(container_id, now=utc_now())

    def list(
        self,
        *,
        workspace_id: str | None = None,
        statuses: tuple[ContainerStatus, ...] = (),
        app_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
    ) -> list[ContainerRecord]:
        normalized_statuses = tuple(sorted(set(statuses), key=lambda item: item.value))
        status_values = tuple(status.value for status in normalized_statuses)
        with self.context.database.session() as session:
            repository = ContainerRepository(session)
            records = (
                repository.list(
                    workspace_id=workspace_id,
                    statuses=status_values,
                    app_id=app_id,
                    stub_ids=stub_ids,
                )
                if workspace_id is not None
                else repository.list_across_workspaces(
                    statuses=status_values,
                    app_id=app_id,
                    stub_ids=stub_ids,
                )
            )
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def page(
        self,
        *,
        workspace_id: str,
        statuses: tuple[ContainerStatus, ...] = (),
        app_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
        cursor: str | None = None,
        limit: int = 100,
    ) -> ContainerPageResult:
        normalized_statuses = tuple(sorted(set(statuses), key=lambda item: item.value))
        normalized_stub_ids = tuple(sorted(set(stub_ids)))
        cursor_scope = _container_cursor_scope(
            workspace_id=workspace_id,
            statuses=normalized_statuses,
            app_id=app_id,
            stub_ids=normalized_stub_ids,
        )
        try:
            decoded_cursor = _decode_container_cursor(cursor, scope=cursor_scope)
        except (binascii.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidInputError("invalid container cursor") from exc
        with self.context.database.session() as session:
            page = ContainerRepository(session).page(
                workspace_id=workspace_id,
                statuses=tuple(status.value for status in normalized_statuses),
                app_id=app_id,
                stub_ids=normalized_stub_ids,
                cursor=decoded_cursor,
                limit=limit,
            )
        return ContainerPageResult(
            data=page.data,
            next=(
                _encode_container_cursor(page.next, scope=cursor_scope)
                if page.next is not None
                else ""
            ),
        )

    def stop(
        self,
        container_id: str,
        *,
        reason: StopContainerReason = StopContainerReason.User,
    ) -> ContainerRecord:
        record = self.get(container_id)
        terminal_statuses = {
            ContainerStatus.Exited,
            ContainerStatus.Failed,
            ContainerStatus.Stopped,
        }
        state_changed = record.status not in terminal_statuses
        if state_changed:
            cancellation = self._cancel_scheduler_request(record.id)
            if cancellation.worker_stop_required:
                self._send_stop_event(
                    record.id,
                    worker_id=cancellation.worker_id,
                    reason=reason,
                )
            if record.task_id:
                self.tasks.cancel(record.task_id)
            record.status = ContainerStatus.Stopped
            record.finished_at = utc_now()
        with self.context.database.session() as session:
            updated = ContainerRepository(session).records.upsert(
                record,
                workspace_id=record.workspace_id,
                name=record.name,
                status=record.status.value,
            )
        self.events.emit(
            "container.stopped",
            resource_type="container",
            resource_id=record.id,
            message=f"stopped container {record.name}",
            workspace_id=record.workspace_id,
        )
        if state_changed:
            self.publish_lifecycle_change(updated, WorkspaceChangeType.Updated)
        return updated

    def stop_for_workspace_deletion(
        self,
        container_id: str,
        *,
        workspace_id: str,
    ) -> ContainerRecord:
        """Terminate one existing container under workspace deletion authority."""
        record = self.get(container_id)
        if record.workspace_id != workspace_id:
            raise NotFoundError(f"container not found: {container_id}")
        terminal_statuses = {
            ContainerStatus.Exited,
            ContainerStatus.Failed,
            ContainerStatus.Stopped,
        }
        if record.status in terminal_statuses:
            return record
        cancellation = self._cancel_scheduler_request(record.id)
        if cancellation.worker_stop_required:
            self._send_stop_event(
                record.id,
                worker_id=cancellation.worker_id,
                reason=StopContainerReason.Admin,
            )
        record.status = ContainerStatus.Stopped
        record.finished_at = utc_now()
        with self.context.database.session() as session:
            return ContainerRepository(session).stop_for_workspace_deletion(
                record,
                now=record.finished_at,
            )

    def delete(self, container_id: str) -> None:
        record = self.get(container_id)
        with self.context.database.session() as session:
            ContainerRepository(session).records.delete(
                container_id,
                workspace_id=record.workspace_id,
            )
        self.publish_lifecycle_change(record, WorkspaceChangeType.Deleted)

    def publish_lifecycle_change(
        self,
        container: ContainerRecord,
        change: WorkspaceChangeType,
    ) -> None:
        if not container.workspace_id:
            return
        self.workspace_changes.emit_change(
            workspace_id=container.workspace_id,
            topic=WorkspaceChangeTopic.Containers,
            change=change,
            resource_id=container.id,
            app_id=container.app_id,
            stub_id=container.stub_id,
            task_id=container.task_id,
            container_id=container.id,
        )

    def _cancel_scheduler_request(
        self,
        container_id: str,
    ) -> SchedulerContainerCancellationResult:
        return self.scheduler_cancellation.cancel(container_id)

    def _send_stop_event(
        self,
        container_id: str,
        *,
        worker_id: str,
        reason: StopContainerReason,
    ) -> None:
        event = EventBusEvent(
            type=EventBusEventType.StopContainer,
            args={
                "container_id": container_id,
                "force": False,
                "reason": reason.value,
                "worker_id": worker_id,
            },
            retries=3,
        )
        self.event_bus.send(event)


def _env_mapping_to_list(env: dict[str, str]) -> list[str]:
    return [f"{key}={value}" for key, value in env.items()]


def _valid_ports(ports: Iterable[int] | None) -> list[int]:
    return [int(port) for port in ports or []]


def _container_cursor_scope(
    *,
    workspace_id: str,
    statuses: tuple[ContainerStatus, ...],
    app_id: str | None,
    stub_ids: tuple[str, ...],
) -> str:
    raw = json.dumps(
        {
            "workspace_id": workspace_id,
            "statuses": [status.value for status in statuses],
            "app_id": app_id,
            "stub_ids": stub_ids,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _encode_container_cursor(cursor: ContainerPageCursor, *, scope: str) -> str:
    raw = (
        ContainerCursorPayload(
            created_at=cursor.created_at,
            id=UUID(cursor.id),
            scope=scope,
        )
        .model_dump_json()
        .encode()
    )
    return base64.urlsafe_b64encode(raw).decode()


def _decode_container_cursor(value: str | None, *, scope: str) -> ContainerPageCursor | None:
    if value is None or value == "":
        return None
    payload = ContainerCursorPayload.model_validate_json(
        base64.urlsafe_b64decode(value.encode()).decode()
    )
    if payload.scope != scope:
        raise ValueError("container cursor does not match requested filters")
    if payload.created_at.tzinfo is None:
        raise ValueError("container cursor timestamp must include a timezone")
    return ContainerPageCursor(created_at=payload.created_at, id=str(payload.id))
