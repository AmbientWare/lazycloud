from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from control.releases import DeploymentReleaseService
from coordination.event_bus import EventBusEvent, EventBusEventType, EventBusSendResult
from database.repositories.apps import StubRepository
from database.repositories.execution import TaskRepository
from database.repositories.images import ImageArchiveRepository, ImageBuildRepository
from database.repositories.orchestration import (
    AutoscalingTargetRepository,
    ContainerPageCursor,
    ContainerRepository,
)
from database.repositories.storage import VolumeRepository
from database.types import DatabaseSession
from foundation.ids import optional_uuid
from observability.events import EventService
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import Field
from shared.autoscaler_state import autoscaler_target_kind
from shared.container_requests import (
    ContainerShutdownTarget,
    OciRuntimeName,
    StopContainerReason,
    WorkerContainerRequestPayload,
    WorkerStartupKind,
)
from shared.containers import TERMINAL_CONTAINER_STATUSES, ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.deployments import StubKind
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.events import EventLevel
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.image_building.records import BuildStatus
from shared.placement import ProductRegion
from shared.scheduling import (
    SchedulerContainerCancellationResult,
    SchedulerContainerSubmitResult,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    gpu_count_for_capacity,
)
from shared.tasks import TaskStatus
from shared.timestamps import utc_now

from execution.admission import PaymentAdmission
from execution.containers.planning import (
    DEFAULT_CONTAINER_DISK_MIB,
    ContainerSchedulingOptions,
    resolve_oci_runtime,
    validate_checkpoint_request,
)
from execution.containers.runtime_state import ContainerRuntimeStateRepository
from execution.context import ExecutionContext
from execution.task_claims import TaskClaimReleaseService
from execution.tasks import TaskService

LOGGER = logging.getLogger(__name__)


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
    def cancel(
        self, container_id: str, *, only_if_pending: bool = False
    ) -> SchedulerContainerCancellationResult: ...


class ContainerEventBus(Protocol):
    def send(self, event: EventBusEvent) -> EventBusSendResult: ...


class ContainerStorageShutdown(Protocol):
    def confirm(
        self, targets: list[ContainerShutdownTarget], *, timeout_seconds: float = 30.0
    ) -> None: ...


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
    region: ProductRegion | None = Field(default=None, exclude=True)
    availability_zone: str = Field(default="", exclude=True)
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
    gpu: list[str] = Field(default_factory=list)
    gpu_count: int = Field(default=0, ge=0)
    timeout_seconds: int = 0
    expires_at: datetime | None = None
    created_at: datetime | None = None


class RuntimeWorkerLookup(Protocol):
    def get_worker(self, worker_id: str) -> SchedulerWorkerRecord | None: ...


@dataclass(slots=True)
class ContainerService:
    context: ExecutionContext
    events: EventService
    tasks: TaskService
    app_admission: AppExecutionAdmission
    payment_admission: PaymentAdmission
    scheduler: ContainerScheduler
    scheduler_cancellation: SchedulerContainerCancellation
    event_bus: ContainerEventBus
    workspace_changes: WorkspaceChangePublisher
    container_shutdowns: ContainerStorageShutdown
    workers: RuntimeWorkerLookup
    runtime_state: ContainerRuntimeStateRepository | None = None

    def admit_container_start(
        self,
        session: DatabaseSession,
        *,
        workspace_id: str,
        gpu: Sequence[str],
        gpu_count: int,
        region: ProductRegion | None = None,
        availability_zone: str = "",
        stub_id: str | None = None,
    ) -> list[str]:
        """Refuse a start the account may not make, before anything exists.

        Returns the GPU models the container is to be scheduled with; the caller
        stores them on the record and asks the scheduler for exactly those.

        Exposed here rather than left to callers to find, because the two that
        build their own container record instead of reserving one still have to
        ask — and asking through the service that owns containers keeps the one
        answer in one place. The count is asked as the number of cards the
        scheduler will hold, so the plan counts what the fleet counts.
        """

        gpu_models = self.payment_admission.admit_container_start(
            session,
            workspace_id=workspace_id,
            gpu=gpu,
            gpu_count=gpu_count_for_capacity(gpu, gpu_count),
            region=region,
            availability_zone=availability_zone,
        )
        if stub_id is not None:
            stub = StubRepository(session).get(stub_id, workspace_id=workspace_id)
            if stub is None:
                raise NotFoundError(f"stub not found: {stub_id}")
            VolumeRepository(session).lock_mounts(
                {mount.name or mount.id for mount in stub.config.volumes},
                workspace_id=workspace_id,
            )
        return gpu_models

    def reserve_pending(
        self,
        session: DatabaseSession,
        reservation: PendingContainerReservation,
    ) -> ContainerRecord:
        # Before the row. Asked ahead of the app check because it is the broader
        # refusal. An account that may not start this stops work whether or not
        # an app owns it, and a reservation without an app id skips the check
        # below entirely.
        gpu = self.admit_container_start(
            session,
            workspace_id=reservation.workspace_id,
            gpu=reservation.gpu,
            gpu_count=reservation.gpu_count,
            region=reservation.region,
            availability_zone=reservation.availability_zone,
            stub_id=reservation.stub_id,
        )
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
        values["gpu"] = gpu
        values["gpu_count"] = gpu_count_for_capacity(gpu, reservation.gpu_count)
        return ContainerRepository(session).records.create(
            values,
            workspace_id=reservation.workspace_id,
            name=reservation.name,
            status=ContainerStatus.Pending.value,
        )

    def reserve_image_build_container(
        self,
        *,
        container_id: str,
        workspace_id: str,
        image_id: str,
    ) -> ContainerRecord:
        """Record the container an image build is about to run in.

        A build holds a worker's cpu and memory for as long as it runs and is
        billed for them, and the control plane prices a container from the
        placement it recorded against this row. Without it a build is capacity
        the platform gave away with nothing durable naming what ran, and its
        usage arrives bounded by no lifetime the control plane can vouch for.

        Owns its session because a build reaches here outside the transaction an
        ordinary container is reserved in; the solvency refusal `reserve_pending`
        makes is the same one, asked before the build costs anything.
        """

        with self.context.database.session() as session:
            build = ImageBuildRepository(session).lock_build(
                container_id, workspace_id=workspace_id
            )
            if build.status not in {BuildStatus.Pending, BuildStatus.Running}:
                raise ConflictError("image build is no longer active")
            if build.image_id != image_id:
                raise ConflictError("image build container image does not match")
            repository = ContainerRepository(session)
            repository.lock_reservation(container_id)
            existing = repository.get_across_workspaces(container_id)
            reservation = PendingContainerReservation(
                id=container_id,
                name=f"image-build-{container_id}",
                image=image_id,
                command=[],
                workspace_id=workspace_id,
            )
            if existing is not None:
                if existing.workspace_id != workspace_id or existing.image != image_id:
                    raise ConflictError("image build container identity does not match")
                return existing
            record = self.reserve_pending(
                session,
                reservation,
            )
        self.publish_lifecycle_change(record, WorkspaceChangeType.Created)
        return record

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
        gpu: Sequence[str] = (),
        gpu_count: int = 0,
        pool_selector: str = "",
        region: ProductRegion | None = None,
        availability_zone: str = "",
        runtime: OciRuntimeName | str = OciRuntimeName.Runsc,
        runtime_class: str = "",
        docker_enabled: bool = False,
        block_network: bool = False,
        allow_list: Iterable[str] | None = None,
        preemptible: bool = False,
        workspace_gpu_quota: int = 0,
        workspace_cpu_quota_millicores: int = 0,
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
                    region=region,
                    availability_zone=availability_zone,
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
                    gpu=list(gpu),
                    gpu_count=gpu_count,
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
                gpu=list(record.gpu),
                gpu_count=record.gpu_count,
                pool_selector=pool_selector,
                region=region,
                availability_zone=availability_zone,
                runtime=runtime,
                runtime_class=runtime_class,
                docker_enabled=docker_enabled,
                block_network=block_network,
                allow_list=list(allow_list) if allow_list is not None else None,
                preemptible=preemptible,
                workspace_gpu_quota=workspace_gpu_quota,
                workspace_cpu_quota_millicores=workspace_cpu_quota_millicores,
            ),
        )
        if not submitted.accepted:
            task = self.tasks.transition(task, TaskStatus.Failed, error=submitted.reason)
            record.status = ContainerStatus.Failed
            record.exit_code = 1
            record.finished_at = utc_now()
            self._release_runtime_state(record)
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
        submitted: SchedulerContainerSubmitResult | None = None
        try:
            request = self._scheduler_request(record, options)
            submitted = self.scheduler.submit(request, ready_at=options.ready_at)
            return submitted
        finally:
            if submitted is None or not submitted.accepted:
                self.stop(record.id, reason=StopContainerReason.Scheduler)

    def _scheduler_request(
        self,
        record: ContainerRecord,
        options: ContainerSchedulingOptions,
    ) -> SchedulerWorkerRequest:
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
                gpu_count_for_capacity(options.gpu, options.gpu_count),
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
            mounts=list(options.mounts or []),
            secret_names=[str(name) for name in options.secret_names or []],
            gateway_token_required=options.gateway_token_required,
            workspace_storage_required=options.workspace_storage_required,
            workspace_storage_available=options.workspace_storage_available,
            workspace_storage_base_mount_path=options.workspace_storage_base_mount_path,
            # A ceiling is a per-container limit rather than scheduled capacity, so
            # it travels in the worker payload and not on the scheduler request.
            # Placement reserves the request; only the runtime enforces these.
            disk_limit_bytes=options.disk_mib * 1024 * 1024,
            cpu_limit_millicores=options.cpu_limit_millicores,
            memory_limit_bytes=(
                options.memory_limit_mib * 1024 * 1024 if options.memory_limit_mib else None
            ),
        )
        request = SchedulerWorkerRequest(
            workspace_id=record.workspace_id,
            stub_id=record.stub_id or options.stub_type or "containers",
            deployment_id=options.deployment_id or "",
            container_id=record.id,
            cpu_millicores=options.cpu_millicores,
            memory_mib=options.memory_mib,
            gpu=list(options.gpu),
            gpu_count=options.gpu_count,
            pool_selector=options.pool_selector,
            region=options.region,
            availability_zone=options.availability_zone,
            runtime_class=runtime_constraint,
            docker_enabled=options.docker_enabled,
            preemptible=options.preemptible,
            workspace_gpu_quota=options.workspace_gpu_quota,
            workspace_cpu_quota_millicores=options.workspace_cpu_quota_millicores,
            payload=payload.model_dump(mode="json"),
        )
        try:
            stub_kind = StubKind(options.stub_type)
        except ValueError:
            stub_kind = None
        target_kind = autoscaler_target_kind(stub_kind) if stub_kind is not None else None
        if record.stub_id and target_kind is not None:
            with self.context.database.session() as session:
                AutoscalingTargetRepository(session).activate(
                    stub_id=record.stub_id,
                    workspace_id=record.workspace_id,
                    target_kind=target_kind,
                )
        return request

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
            return self.get_in_session(session, container_id)

    def accepting_work(self, container_id: str) -> bool:
        container = self.get(container_id)
        worker = self.workers.get_worker(container.runtime_worker_id or "")
        return worker is not None and bool(DeploymentReleaseService().admitted_workers([worker]))

    def get_in_session(
        self,
        session: DatabaseSession,
        container_id: str,
    ) -> ContainerRecord:
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

    # Why a container stopped decides what happens to the work it was holding.
    # These are the reasons where the work itself is over: the caller asked for it
    # to stop, or an operator did. Everything else is the platform moving capacity
    # around — the invocation is still wanted, and belongs back in the pool.
    _CLAIM_CANCELLING_REASONS = frozenset(
        {
            StopContainerReason.User,
            StopContainerReason.Admin,
            StopContainerReason.Unfunded,
        }
    )

    def _settle_claimed_work(
        self,
        record: ContainerRecord,
        *,
        reason: StopContainerReason,
    ) -> None:
        """Decide what becomes of the invocations this container had claimed.

        A pooled container holds somebody's in-flight call. Losing the claim
        silently is the failure this exists to prevent: the task would keep
        naming a container that is gone, no claim would ever see it again, and
        the caller would wait on a result nothing was left to produce.

        Released rather than cancelled wherever the stop was the platform's idea.
        Scaling down, reclaiming a preemptible node or expiring a keep-warm
        window are all reasons to run the call elsewhere, never reasons to tell
        the caller their function was cancelled.
        """

        with self.context.database.session() as session:
            held = TaskRepository(session).list_inflight_for_container(record.id)
        # Both directions of the association, because they can disagree. A
        # container started for one task names it before that task names back;
        # a pooled container is named only by whatever it later claimed.
        task_ids = {task.id for task in held}
        if record.task_id:
            task_ids.add(record.task_id)
        if reason in self._CLAIM_CANCELLING_REASONS:
            for task_id in task_ids:
                self.tasks.cancel(task_id)
            return
        for task_id in task_ids:
            with self.context.database.session() as session:
                TaskClaimReleaseService(session).release(task_id, container_id=record.id)

    def stop(
        self,
        container_id: str,
        *,
        reason: StopContainerReason | None = None,
        only_if_pending: bool = False,
    ) -> ContainerRecord:
        """Stop a container, recording why if the caller said.

        `None` is not the same as `User` for the customer, who would otherwise
        be told they stopped something they had nothing to do with. It is the
        same for everything else: the reason on the wire decides how the worker
        normalizes an exit and how claimed work settles, and `Unknown` there
        means the container died on its own rather than that nobody said why.
        So an unstated stop still travels as `User` and only declines to make a
        claim about who did it.
        """

        record = self.get(container_id)
        if only_if_pending and record.status is not ContainerStatus.Pending:
            return record
        settlement_reason = StopContainerReason.User if reason is None else reason
        if record.status in TERMINAL_CONTAINER_STATUSES:
            # Already over. Announcing would write a fresh cause across the one
            # that actually ended it, and rewriting the row would cost a session
            # to change nothing.
            return record
        cancellation = self._cancel_scheduler_request(record.id, only_if_pending=only_if_pending)
        if not cancellation.cancelled:
            return self.get(container_id)
        if cancellation.worker_stop_required:
            self._send_stop_event(
                record.id,
                worker_id=cancellation.worker_id,
                reason=settlement_reason,
            )
        with self.context.database.session() as session:
            containers = ContainerRepository(session)
            current = containers.get_across_workspaces(container_id)
            if current is None:
                raise NotFoundError(f"container not found: {container_id}")
            if current.status in TERMINAL_CONTAINER_STATUSES:
                # The worker's report landed while this was deciding, and it
                # says what actually happened: a full-payload write from the
                # read above would put a stale exit code back over it.
                updated = current
            else:
                current.status = ContainerStatus.Stopped
                current.finished_at = utc_now()
                # Written here rather than left to the worker's exit report. A
                # container that never reached a worker — still pending, or its
                # worker already gone — is never reported on, and the reason it
                # was stopped for is the one its owner most needs. Never back to
                # Unknown, matching the worker-report writer.
                if reason is not None and reason is not StopContainerReason.Unknown:
                    current.termination_reason = reason
                updated = containers.records.upsert(
                    current,
                    workspace_id=current.workspace_id,
                    name=current.name,
                    status=current.status.value,
                )
        # Settlement runs whether or not this call was the one that wrote the
        # terminal row. It is the only thing that cancels the claims a `User`,
        # `Admin` or `Unfunded` stop is asked to cancel, and losing the race to
        # the worker — which only ever releases them — would put the work back
        # in the pool for the stop to fail to stop.
        #
        # After the row is terminal, never before. A claim is refused from a
        # container the record calls terminal, so settling first opens a
        # window where the work is free and this container still reads as
        # live — it takes back what it just gave up and then goes away
        # holding it.
        self._settle_claimed_work(updated, reason=settlement_reason)
        self._release_runtime_state(updated)
        cause = "" if reason is None else reason.describe()
        self.events.emit(
            "container.stopped",
            resource_type="container",
            resource_id=updated.id,
            message=f"stopped container {updated.name}" + (f": {cause}" if cause else ""),
            level=(
                EventLevel.Warning if reason is StopContainerReason.Unfunded else EventLevel.Info
            ),
            # What the stop was asked for, which is not always what the row
            # settles on: the worker reports what it actually observed. Absent
            # when the caller named nothing, rather than naming the settlement
            # default the message declines to give.
            data={} if reason is None else {"stop_reason": reason.value},
            workspace_id=updated.workspace_id,
        )
        self.publish_lifecycle_change(updated, WorkspaceChangeType.Updated)
        return updated

    def _release_runtime_state(self, record: ContainerRecord) -> None:
        """Give up the Redis state this container held.

        Best effort on purpose: the container is already terminal in the record
        that matters, and refusing to persist that because a cache write failed
        would trade a stale key for a container stuck Running forever. Whatever
        is left behind is reclaimed when the app or workspace is deleted.
        """
        if self.runtime_state is None or not record.stub_id:
            return
        try:
            self.runtime_state.release(
                workspace_id=record.workspace_id,
                stub_id=record.stub_id,
                container_id=record.id,
            )
        except Exception:
            LOGGER.warning(
                "releasing container runtime state failed",
                exc_info=True,
                extra={"container_id": record.id},
            )

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
        if record.status in TERMINAL_CONTAINER_STATUSES:
            return record
        cancellation = self._cancel_scheduler_request(record.id)
        if cancellation.worker_stop_required:
            self._send_stop_event(
                record.id,
                worker_id=cancellation.worker_id,
                reason=StopContainerReason.Admin,
            )
        # Deliberately does not settle what the container was holding, unlike
        # every other stop. A deleting workspace accepts no task writes at all,
        # and the claims do not outlive it either way: finalization deletes every
        # row the workspace owns, the tasks among them.
        record.status = ContainerStatus.Stopped
        record.finished_at = utc_now()
        with self.context.database.session() as session:
            return ContainerRepository(session).stop_for_workspace_deletion(
                record,
                now=record.finished_at,
            )

    def delete(self, container_id: str) -> None:
        record = self.get(container_id)
        if record.status in {ContainerStatus.Pending, ContainerStatus.Running}:
            # The row is what authorizes the worker's usage writes and what the
            # ledger prices the placement from, so deleting it under a live
            # container silently ends metering while the work goes on running.
            raise ConflictError(
                f"container {container_id} is {record.status.value}: stop it before deleting"
            )
        worker_id = record.runtime_worker_id or record.worker_id or ""
        if not worker_id:
            cancellation = self._cancel_scheduler_request(record.id)
            worker_id = cancellation.worker_id if cancellation.worker_stop_required else ""
        if worker_id:
            self.container_shutdowns.confirm(
                [ContainerShutdownTarget(container_id=record.id, worker_id=worker_id)]
            )
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
        *,
        only_if_pending: bool = False,
    ) -> SchedulerContainerCancellationResult:
        return self.scheduler_cancellation.cancel(container_id, only_if_pending=only_if_pending)

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
