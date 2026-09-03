from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

from compute.service import ComputeService
from database.records.apps import AppRecord, AutoscalingStubRecord, StubRecord
from database.types import DatabaseSession
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue
from shared.autoscaler_state import AutoscalerStateRecord, AutoscalerTargetKind
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.cron import CronJobRecord
from shared.deployment_records import Deployment
from shared.events import Event, EventLevel
from shared.http.workspace_changes import WorkspaceChangeType
from shared.identity import WorkspaceRecord

from database import DatabaseClient


class SchedulerContext(Protocol):
    @property
    def database(self) -> DatabaseClient: ...

    def workspace(
        self,
        session: DatabaseSession,
        workspace: str = "default",
    ) -> WorkspaceRecord: ...

    def default_workspace_id(self, session: DatabaseSession) -> str: ...


class SchedulerEventService(Protocol):
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

    def list(
        self,
        *,
        workspace_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        actions: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[Event]: ...

    def prune(
        self,
        *,
        retention: timedelta = ...,
        telemetry_retention: timedelta = ...,
    ) -> int: ...


class SchedulerMetricsService(Protocol):
    def increment(
        self,
        name: str,
        amount: float = 1,
        *,
        labels: dict[str, str] | None = None,
    ) -> None: ...

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None: ...


class SchedulerAutoscalerStateService(Protocol):
    def upsert(self, state: AutoscalerStateRecord) -> AutoscalerStateRecord: ...

    def get(
        self,
        *,
        workspace_id: str,
        target_kind: AutoscalerTargetKind,
        target_id: str,
    ) -> AutoscalerStateRecord | None: ...

    def list(
        self,
        *,
        workspace_id: str | None = None,
        source: str | None = None,
    ) -> list[AutoscalerStateRecord]: ...


class SchedulerDeploymentService(Protocol):
    def get(self, deployment_id_or_name: str) -> Deployment: ...


class SchedulerAppLifecycleService(Protocol):
    def get(self, app_id_or_name: str, *, workspace: str | None = None) -> AppRecord: ...

    def reconcile_pending(self, *, limit: int = 25) -> list[AppRecord]: ...


class SchedulerCronJobService(Protocol):
    def list(self, *, workspace: str = "default") -> list[CronJobRecord]: ...

    def list_all(self) -> list[CronJobRecord]: ...

    def publish_change(
        self,
        record: CronJobRecord,
        change: WorkspaceChangeType,
    ) -> None: ...


class SchedulerCollectionService(Protocol):
    def queue_depth(self, queue: str, *, workspace_id: str | None = None) -> int: ...


class SchedulerContainerService(Protocol):
    def list(
        self,
        *,
        workspace_id: str | None = None,
        statuses: tuple[ContainerStatus, ...] = (),
        app_id: str | None = None,
        stub_ids: tuple[str, ...] = (),
    ) -> list[ContainerRecord]: ...

    def stop(
        self,
        container_id: str,
        *,
        reason: StopContainerReason = StopContainerReason.User,
    ) -> ContainerRecord: ...


class SchedulerWorkloadDirectory(Protocol):
    def list_stubs(self, *, workspace: str | None = None) -> list[StubRecord]: ...

    def list_autoscaling_stubs(self) -> list[AutoscalingStubRecord]: ...

    def get_stub(
        self,
        stub_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> StubRecord: ...

    def get_workspace(self, workspace: str = "default") -> WorkspaceRecord: ...

    def set_autoscaling_enabled(
        self,
        stub_id_or_name: str,
        *,
        workspace: str,
        enabled: bool,
    ) -> StubRecord: ...


class SchedulerServices(Protocol):
    @property
    def context(self) -> SchedulerContext: ...

    @property
    def events(self) -> SchedulerEventService: ...

    @property
    def metrics(self) -> SchedulerMetricsService: ...

    @property
    def autoscaler_states(self) -> SchedulerAutoscalerStateService: ...

    @property
    def apps(self) -> SchedulerAppLifecycleService: ...

    @property
    def deployments(self) -> SchedulerDeploymentService: ...

    @property
    def cron_jobs(self) -> SchedulerCronJobService: ...

    @property
    def collections(self) -> SchedulerCollectionService: ...

    @property
    def containers(self) -> SchedulerContainerService: ...

    @property
    def scheduler_workloads(self) -> SchedulerWorkloadDirectory: ...

    @property
    def compute(self) -> ComputeService: ...

    @property
    def workspace_changes(self) -> WorkspaceChangePublisher: ...
