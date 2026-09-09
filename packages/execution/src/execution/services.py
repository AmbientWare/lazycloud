from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from control.apps import AppReader
from database.types import DatabaseSession
from observability.events import EventService
from observability.metrics import MetricsService
from observability.usage import UsageService
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import Deployment
from shared.http.workspace_changes import WorkspaceChangeType
from shared.placement import ProductRegion
from shared.scheduling import SchedulerContainerSubmitStatus
from storage.service import ObjectStorage

from execution.containers.planning import ContainerSchedulingOptions
from execution.containers.service import PendingContainerReservation
from execution.context import ExecutionContext
from execution.tasks import TaskService


class SchedulerSubmissionResult(Protocol):
    @property
    def accepted(self) -> bool: ...

    @property
    def status(self) -> SchedulerContainerSubmitStatus: ...

    @property
    def reason(self) -> str | None: ...


class ExecutionContainerService(Protocol):
    def reserve_image_build_container(
        self, *, container_id: str, workspace_id: str, image_id: str
    ) -> ContainerRecord: ...

    def reserve_pending(
        self,
        session: DatabaseSession,
        reservation: PendingContainerReservation,
    ) -> ContainerRecord: ...

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
    ) -> list[str]: ...

    def get(self, container_id: str) -> ContainerRecord: ...

    def submit_scheduler_request(
        self,
        record: ContainerRecord,
        options: ContainerSchedulingOptions,
    ) -> SchedulerSubmissionResult: ...

    # The reason is part of the request, not a detail of the implementation: it
    # is what decides whether the invocations the container was holding are
    # cancelled or handed back. A caller that cannot say why it is stopping a
    # container cannot say what should become of somebody else's work.
    def stop(
        self,
        container_id: str,
        *,
        reason: StopContainerReason = StopContainerReason.User,
    ) -> ContainerRecord: ...

    def delete(self, container_id: str) -> None: ...

    def unsettled_preemptions(self, *, limit: int) -> list[ContainerRecord]: ...

    def mark_preemption_settled(self, container_id: str) -> None: ...

    def publish_lifecycle_change(
        self,
        container: ContainerRecord,
        change: WorkspaceChangeType,
    ) -> None: ...

    def list(
        self,
        *,
        workspace_id: str | None = None,
        statuses: tuple[ContainerStatus, ...] = (),
        app_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
    ) -> list[ContainerRecord]: ...


class ExecutionLookupService(Protocol):
    def get(self, deployment_id_or_name: str) -> Deployment: ...


class ExecutionServices(Protocol):
    @property
    def context(self) -> ExecutionContext: ...

    @property
    def events(self) -> EventService: ...

    @property
    def tasks(self) -> TaskService: ...

    @property
    def containers(self) -> ExecutionContainerService: ...

    @property
    def usage(self) -> UsageService: ...

    @property
    def metrics(self) -> MetricsService: ...

    @property
    def deployments(self) -> ExecutionLookupService: ...

    @property
    def apps(self) -> AppReader: ...

    @property
    def object_storage(self) -> ObjectStorage: ...
