from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from compute.service import ComputeServices
from control.apps import AppReader
from control.cron_jobs import CronJobService
from control.service import ControlServices
from database.records.apps import AppRecord
from database.types import DatabaseSession
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import Deployment
from shared.events import Event, EventLevel
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


class SchedulerDeploymentService(Protocol):
    def get(self, deployment_id_or_name: str) -> Deployment: ...


class SchedulerAppLifecycleService(Protocol):
    def get(self, app_id_or_name: str, *, workspace: str | None = None) -> AppRecord: ...

    def reconcile_pending(self, *, limit: int = 25) -> list[AppRecord]: ...


class SchedulerDeploymentPruneService(Protocol):
    def reconcile_pending(self, *, limit: int = 25) -> None: ...


class SchedulerContainerService(Protocol):
    def expire_containers(self, *, now: datetime | None = None) -> list[ContainerRecord]: ...

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
        only_if_pending: bool = False,
        only_if_unassigned: bool = False,
    ) -> ContainerRecord: ...


class SchedulerServices(Protocol):
    @property
    def context(self) -> SchedulerContext: ...

    @property
    def events(self) -> SchedulerEventService: ...

    @property
    def metrics(self) -> SchedulerMetricsService: ...

    @property
    def apps(self) -> AppReader: ...

    @property
    def deployments(self) -> SchedulerDeploymentService: ...

    @property
    def cron_jobs(self) -> CronJobService: ...

    @property
    def containers(self) -> SchedulerContainerService: ...

    @property
    def control_plane_service(self) -> ControlServices: ...

    @property
    def workspace_changes(self) -> WorkspaceChangePublisher: ...


class FleetServices(Protocol):
    @property
    def control_plane_service(self) -> ControlServices: ...

    @property
    def context(self) -> SchedulerContext: ...

    @property
    def events(self) -> SchedulerEventService: ...

    @property
    def metrics(self) -> SchedulerMetricsService: ...

    @property
    def apps(self) -> SchedulerAppLifecycleService: ...

    @property
    def deployment_plans(self) -> SchedulerDeploymentPruneService: ...

    @property
    def containers(self) -> SchedulerContainerService: ...

    @property
    def compute(self) -> ComputeServices: ...
