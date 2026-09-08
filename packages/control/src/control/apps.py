from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from database.records.apps import AppRecord, StubRecord
from database.repositories.apps import (
    AppContainerShutdownIntentRepository,
    AppDeploymentIntentRepository,
    AppRepository,
    DeploymentRepository,
    StubRepository,
)
from database.repositories.cleanup import CleanupRepository
from database.repositories.execution import EventRepository
from database.repositories.orchestration import AutoscalingTargetRepository
from foundation.ids import try_uuid
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import JsonValue
from shared.app_lifecycle import (
    UNFINISHED_APP_LIFECYCLE_STATES,
    AppDeploymentIntentTarget,
    AppLifecycleState,
    AppLifecycleTarget,
)
from shared.app_slug import validate_app_slug
from shared.autoscaler_state import autoscaler_target_kind
from shared.container_requests import ContainerShutdownTarget
from shared.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.events import Event
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from control.context import ControlContext
from control.deployment_cleanup import AppDeploymentLifecycleService

LOGGER = logging.getLogger(__name__)

_RECONCILE_CLAIM_SECONDS = 30


class AppReader(Protocol):
    def get(self, app_id_or_name: str, *, workspace: str | None = None) -> AppRecord: ...


class AppRegistry(AppReader, Protocol):
    def create(
        self,
        name: str,
        *,
        stub_id: str | None = None,
        workspace: str = "default",
        version: int = 1,
        public: bool = False,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> AppRecord: ...

    def list(
        self,
        *,
        workspace: str | None = None,
        active: bool | None = None,
    ) -> list[AppRecord]: ...


class AppImageAvailability(Protocol):
    def assert_available(self, session: Session, stub_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DatabaseAppImageAvailability:
    def assert_available(self, session: Session, stub_id: str) -> None:
        CleanupRepository(session).assert_stub_available(stub_id)


@dataclass(frozen=True, slots=True)
class DatabaseAppExecutionAdmission:
    def assert_active(
        self,
        session: Session,
        *,
        app_id: str,
        workspace_id: str,
    ) -> None:
        app = AppRepository(session).get_for_update(app_id, workspace_id=workspace_id)
        if app is None:
            raise NotFoundError(f"app not found: {app_id}")
        if app.lifecycle_state is not AppLifecycleState.Active:
            raise ConflictError(f"app {app_id} is not active")


class AppExecutionLifecycleEffects(Protocol):
    def stop_app_containers(
        self,
        *,
        workspace_id: str,
        app_id: str,
        container_targets: list[ContainerShutdownTarget],
    ) -> None: ...

    def delete_app_execution(self, *, workspace_id: str, app_id: str) -> None: ...


@dataclass(slots=True)
class AppService:
    context: ControlContext
    deployment_lifecycle: AppDeploymentLifecycleService
    execution_effects: AppExecutionLifecycleEffects
    artifact_availability: AppImageAvailability
    workspace_changes: WorkspaceChangePublisher | None = None

    def create(
        self,
        name: str,
        *,
        stub_id: str | None = None,
        workspace: str = "default",
        version: int = 1,
        public: bool = False,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> AppRecord:
        try:
            app_name = validate_app_slug(name)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            stub = (
                _get_stub(session, stub_id, workspace_id=workspace_record.id) if stub_id else None
            )
            if stub is not None:
                self.artifact_availability.assert_available(session, stub.id)
            repository = AppRepository(session)
            existing = repository.get_by_name(
                app_name,
                workspace_id=workspace_record.id,
                for_update=True,
            )
            change = (
                WorkspaceChangeType.Created if existing is None else WorkspaceChangeType.Updated
            )
            now = utc_now()
            if existing is None:
                record = AppRecord(
                    id=str(uuid4()),
                    workspace_id=workspace_record.id,
                    stub_id=stub.id if stub is not None else None,
                    name=app_name,
                    version=version,
                    public=public,
                    metadata=dict(metadata or {}),
                    created_at=now,
                    updated_at=now,
                )
            else:
                if existing.lifecycle_state in UNFINISHED_APP_LIFECYCLE_STATES:
                    raise ConflictError("app lifecycle operation is in progress")
                record = existing.model_copy(
                    update={
                        "stub_id": stub.id if stub is not None else existing.stub_id,
                        "version": version,
                        "public": public,
                        "metadata": {**existing.metadata, **(metadata or {})},
                        "updated_at": now,
                    }
                )
            record = repository.upsert(record)
            stub_repository = StubRepository(session)
            if stub is not None:
                stub.app_id = record.id
                stub.public = public or stub.public
                stub.updated_at = now
                stub_repository.upsert(stub)
        self._publish_change(record, change)
        if stub is not None:
            self._publish_workload_change(stub, WorkspaceChangeType.Updated)
        return record

    def get(self, app_id_or_name: str, *, workspace: str | None = None) -> AppRecord:
        with self.context.database.session() as session:
            return self.get_in_session(session, app_id_or_name, workspace=workspace)

    def get_in_session(
        self,
        session: Session,
        app_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> AppRecord:
        workspace_id = (
            self.context.workspace(session, workspace).id if workspace is not None else None
        )
        repository = AppRepository(session)
        app_id = try_uuid(app_id_or_name)
        if app_id is not None:
            record = (
                repository.get(app_id, workspace_id=workspace_id)
                if workspace_id is not None
                else repository.get_across_workspaces(app_id)
            )
        elif workspace_id is not None:
            record = repository.get_by_name(app_id_or_name, workspace_id=workspace_id)
        else:
            matches = [
                item for item in repository.list_across_workspaces() if item.name == app_id_or_name
            ]
            record = max(
                matches,
                key=lambda item: (item.version, item.updated_at),
                default=None,
            )
        if record is not None:
            return record
        raise NotFoundError(f"app not found: {app_id_or_name}")

    def list(
        self,
        *,
        workspace: str | None = None,
        active: bool | None = None,
    ) -> list[AppRecord]:
        with self.context.database.session() as session:
            workspace_id = (
                self.context.workspace(session, workspace).id if workspace is not None else None
            )
            repository = AppRepository(session)
            records = (
                repository.list(workspace_id=workspace_id)
                if workspace_id is not None
                else repository.list_across_workspaces()
            )
        if active is not None:
            records = [item for item in records if item.active is active]
        return records

    def pause(self, app_id_or_name: str, *, workspace: str = "default") -> AppRecord:
        return self._mutate(
            app_id_or_name,
            workspace=workspace,
            target=AppLifecycleTarget.Paused,
        )

    def resume(self, app_id_or_name: str, *, workspace: str = "default") -> AppRecord:
        return self._mutate(
            app_id_or_name,
            workspace=workspace,
            target=AppLifecycleTarget.Active,
        )

    def delete(self, app_id_or_name: str, *, workspace: str = "default") -> AppRecord:
        return self._mutate(
            app_id_or_name,
            workspace=workspace,
            target=AppLifecycleTarget.Deleted,
            include_deleted=True,
        )

    def reconcile_pending(self, *, limit: int = 25) -> list[AppRecord]:
        claim_id = str(uuid4())
        with self.context.database.session() as session:
            claimed = AppRepository(session).claim_unfinished(
                claim_id=claim_id,
                stale_before=utc_now() - timedelta(seconds=_RECONCILE_CLAIM_SECONDS),
                limit=limit,
            )
        reconciled: list[AppRecord] = []
        for app in claimed:
            try:
                reconciled.append(self._run_claimed(app, claim_id=claim_id))
            except Exception:
                LOGGER.exception("app %s failed to reconcile", app.id)
                reconciled.append(
                    self._get_including_deleted(app.id, workspace_id=app.workspace_id)
                )
        return reconciled

    def _mutate(
        self,
        app_id_or_name: str,
        *,
        workspace: str,
        target: AppLifecycleTarget,
        include_deleted: bool = False,
    ) -> AppRecord:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            app = _app_for_update(
                session,
                app_id_or_name,
                workspace_id=workspace_id,
                include_deleted=include_deleted,
            )
            if app is None:
                raise NotFoundError(f"app not found: {app_id_or_name}")
            if _terminal_target_matches(app, target):
                return app
            if app.lifecycle_state in UNFINISHED_APP_LIFECYCLE_STATES:
                if app.lifecycle_target is not target:
                    raise ConflictError("app has an incompatible lifecycle operation in progress")
                revision = app.lifecycle_revision
            else:
                revision = app.lifecycle_revision + 1
                app = self._begin_operation(
                    session,
                    app,
                    revision=revision,
                    target=target,
                )
            claim_id = _claim_specific(session, app, revision=revision)
            if claim_id is None:
                return app
        return self._run_claimed(app, claim_id=claim_id)

    def _begin_operation(
        self,
        session: Session,
        app: AppRecord,
        *,
        revision: int,
        target: AppLifecycleTarget,
    ) -> AppRecord:
        deployments = DeploymentRepository(session)
        intents = AppDeploymentIntentRepository(session)
        AppContainerShutdownIntentRepository(session).clear(app_id=app.id)
        if target is AppLifecycleTarget.Paused:
            if app.lifecycle_state is not AppLifecycleState.Active:
                raise ConflictError("only an active app can be paused")
            deployment_ids = [
                item.id
                for item in deployments.list(workspace_id=app.workspace_id, app_id=app.id)
                if item.active
            ]
            intents.replace(
                app_id=app.id,
                deployment_ids=deployment_ids,
                operation_revision=revision,
                target=AppDeploymentIntentTarget.Inactive,
            )
            state = AppLifecycleState.Pausing
        elif target is AppLifecycleTarget.Active:
            if app.lifecycle_state is not AppLifecycleState.Paused:
                raise ConflictError("only a paused app can be resumed")
            intents.retarget(
                app_id=app.id,
                operation_revision=revision,
                target=AppDeploymentIntentTarget.Active,
            )
            state = AppLifecycleState.Resuming
        else:
            if app.lifecycle_state is AppLifecycleState.Deleted:
                return app
            intents.replace(
                app_id=app.id,
                deployment_ids=[
                    item.id
                    for item in deployments.list(
                        workspace_id=app.workspace_id,
                        app_id=app.id,
                    )
                ],
                operation_revision=revision,
                target=AppDeploymentIntentTarget.Deleted,
            )
            state = AppLifecycleState.Deleting
        started = app.model_copy(
            update={
                "lifecycle_state": state,
                "lifecycle_revision": revision,
                "lifecycle_target": target,
                "lifecycle_operation_id": str(uuid4()),
                "lifecycle_failure": None,
                "reconcile_claim_id": None,
                "reconcile_claimed_at": None,
                "lifecycle_event_id": None,
                "lifecycle_event_created_at": None,
                "lifecycle_change_published_at": None,
                "updated_at": utc_now(),
            }
        )
        return AppRepository(session).upsert(started)

    def _run_claimed(self, app: AppRecord, *, claim_id: str) -> AppRecord:
        phase = "deployment-lifecycle"
        try:
            with self.context.database.session() as session:
                current = AppRepository(session).get(
                    app.id,
                    workspace_id=app.workspace_id,
                    include_deleted=True,
                )
                if current is None:
                    raise NotFoundError(f"app not found: {app.id}")
                if current.reconcile_claim_id != claim_id:
                    return current
                intents = AppDeploymentIntentRepository(session).list(app_id=app.id)
            self.deployment_lifecycle.apply_intents(
                app_id=app.id,
                workspace_id=app.workspace_id,
                intents=intents,
            )
            target = app.lifecycle_target
            if target is None:
                raise ConflictError("app lifecycle target is missing")
            if target is not AppLifecycleTarget.Active:
                phase = "container-stop"
                container_targets = self._capture_container_shutdown_intents(app)
                self.execution_effects.stop_app_containers(
                    workspace_id=app.workspace_id,
                    app_id=app.id,
                    container_targets=container_targets,
                )
            if target is AppLifecycleTarget.Deleted:
                phase = "execution-cleanup"
                self.execution_effects.delete_app_execution(
                    workspace_id=app.workspace_id,
                    app_id=app.id,
                )
            phase = "placement-route-cleanup"
            self.deployment_lifecycle.reconcile_placement_and_routes(
                workspace_id=app.workspace_id,
                required=target is AppLifecycleTarget.Active,
            )
            phase = "terminal-event"
            staged = self._stage_terminal_event(app, claim_id=claim_id, target=target)
            phase = "terminal-change"
            return self._publish_and_complete_terminal(
                staged,
                claim_id=claim_id,
                target=target,
            )
        except Exception as exc:
            self._fail_claim(app, claim_id=claim_id, phase=phase, error=exc)
            raise

    def _stage_terminal_event(
        self,
        app: AppRecord,
        *,
        claim_id: str,
        target: AppLifecycleTarget,
    ) -> AppRecord:
        with self.context.database.session() as session:
            repository = AppRepository(session)
            current = repository.get_for_update(
                app.id,
                workspace_id=app.workspace_id,
                include_deleted=True,
            )
            if current is None:
                raise NotFoundError(f"app not found: {app.id}")
            if (
                current.lifecycle_revision != app.lifecycle_revision
                or current.reconcile_claim_id != claim_id
                or current.lifecycle_target is not target
            ):
                return current
            now = utc_now()
            event_id = current.lifecycle_event_id or str(
                uuid5(
                    NAMESPACE_URL,
                    f"lazycloud:app-lifecycle:app:{current.lifecycle_operation_id}:{target.value}",
                )
            )
            event_created_at = current.lifecycle_event_created_at or now
            EventRepository(session).append(
                Event(
                    id=event_id,
                    action=_app_event_action(target),
                    resource_type="app",
                    resource_id=current.id,
                    message=f"{target.value} app {current.name}",
                    created_at=event_created_at,
                ),
                workspace_id=current.workspace_id,
            )
            staged = current.model_copy(
                update={
                    "lifecycle_event_id": event_id,
                    "lifecycle_event_created_at": event_created_at,
                    "updated_at": now,
                }
            )
            return repository.upsert(staged)

    def _publish_and_complete_terminal(
        self,
        app: AppRecord,
        *,
        claim_id: str,
        target: AppLifecycleTarget,
    ) -> AppRecord:
        if app.lifecycle_event_id is None or app.lifecycle_event_created_at is None:
            raise ConflictError("app lifecycle terminal event is incomplete")
        if self.workspace_changes is not None:
            published = self.workspace_changes.emit_change(
                workspace_id=app.workspace_id,
                topic=WorkspaceChangeTopic.Apps,
                change=(
                    WorkspaceChangeType.Deleted
                    if target is AppLifecycleTarget.Deleted
                    else WorkspaceChangeType.Updated
                ),
                resource_id=app.id,
                app_id=app.id,
                stub_id=app.stub_id,
                occurred_at=app.lifecycle_event_created_at,
                event_id=app.lifecycle_event_id,
            )
            if published is None:
                raise UpstreamUnavailableError("app lifecycle change publication is unavailable")
        with self.context.database.session() as session:
            repository = AppRepository(session)
            current = repository.get_for_update(
                app.id,
                workspace_id=app.workspace_id,
                include_deleted=True,
            )
            if current is None:
                raise NotFoundError(f"app not found: {app.id}")
            if (
                current.lifecycle_revision != app.lifecycle_revision
                or current.reconcile_claim_id != claim_id
                or current.lifecycle_target is not target
                or current.lifecycle_event_id != app.lifecycle_event_id
            ):
                return current
            now = utc_now()
            completed = current.model_copy(
                update={
                    "lifecycle_state": AppLifecycleState(target.value),
                    "lifecycle_target": None,
                    "lifecycle_operation_id": None,
                    "lifecycle_failure": None,
                    "reconcile_claim_id": None,
                    "reconcile_claimed_at": None,
                    "lifecycle_change_published_at": now,
                    "updated_at": now,
                    "deleted_at": now if target is AppLifecycleTarget.Deleted else None,
                }
            )
            resumed_deployment_ids = (
                [
                    intent.deployment_id
                    for intent in AppDeploymentIntentRepository(session).list(app_id=app.id)
                ]
                if target is AppLifecycleTarget.Active
                else []
            )
            if target is not AppLifecycleTarget.Paused:
                AppDeploymentIntentRepository(session).clear(app_id=app.id)
            AppContainerShutdownIntentRepository(session).clear(app_id=app.id)
            completed = repository.upsert(completed)
            if target is AppLifecycleTarget.Active:
                targets = AutoscalingTargetRepository(session)
                stubs = StubRepository(session)
                resumed_stubs = stubs.list_for_deployments(
                    resumed_deployment_ids,
                    workspace_id=app.workspace_id,
                )
                current_stub = (
                    stubs.get(completed.stub_id, workspace_id=app.workspace_id)
                    if completed.stub_id is not None
                    else None
                )
                by_id = {stub.id: stub for stub in resumed_stubs}
                if current_stub is not None:
                    by_id[current_stub.id] = current_stub
                for stub in by_id.values():
                    target_kind = autoscaler_target_kind(stub.kind)
                    if target_kind is not None:
                        targets.activate(
                            stub_id=stub.id,
                            workspace_id=stub.workspace_id,
                            target_kind=target_kind,
                        )
            return completed

    def _capture_container_shutdown_intents(
        self,
        app: AppRecord,
    ) -> list[ContainerShutdownTarget]:
        with self.context.database.session() as session:
            intents = AppContainerShutdownIntentRepository(session).capture_pending_shutdowns(
                app_id=app.id,
                workspace_id=app.workspace_id,
                operation_revision=app.lifecycle_revision,
            )
        return [
            ContainerShutdownTarget(
                container_id=intent.container_id,
                worker_id=intent.worker_id,
            )
            for intent in intents
        ]

    def _fail_claim(
        self,
        app: AppRecord,
        *,
        claim_id: str,
        phase: str,
        error: Exception,
    ) -> None:
        with self.context.database.session() as session:
            repository = AppRepository(session)
            current = repository.get_for_update(
                app.id,
                workspace_id=app.workspace_id,
                include_deleted=True,
            )
            if current is None or (
                current.lifecycle_revision != app.lifecycle_revision
                or current.reconcile_claim_id != claim_id
            ):
                return
            repository.upsert(
                current.model_copy(
                    update={
                        "lifecycle_state": AppLifecycleState.CleanupFailed,
                        "lifecycle_failure": f"{phase}: {type(error).__name__}",
                        "reconcile_claim_id": None,
                        "reconcile_claimed_at": None,
                        "reconcile_attempt_count": current.reconcile_attempt_count + 1,
                        "updated_at": utc_now(),
                    }
                )
            )

    def _get_including_deleted(self, app_id: str, *, workspace_id: str) -> AppRecord:
        with self.context.database.session() as session:
            app = AppRepository(session).get(
                app_id,
                workspace_id=workspace_id,
                include_deleted=True,
            )
        if app is None:
            raise NotFoundError(f"app not found: {app_id}")
        return app

    def _publish_change(self, app: AppRecord, change: WorkspaceChangeType) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=app.workspace_id,
            topic=WorkspaceChangeTopic.Apps,
            change=change,
            resource_id=app.id,
            app_id=app.id,
            stub_id=app.stub_id,
        )

    def _publish_workload_change(
        self,
        stub: StubRecord,
        change: WorkspaceChangeType,
    ) -> None:
        if self.workspace_changes is None:
            return
        self.workspace_changes.emit_change(
            workspace_id=stub.workspace_id,
            topic=WorkspaceChangeTopic.Workloads,
            change=change,
            resource_id=stub.id,
            app_id=stub.app_id,
            deployment_id=stub.deployment_id,
            stub_id=stub.id,
        )


def _get_stub(session: Session, stub_id_or_name: str, *, workspace_id: str) -> StubRecord:
    repository = StubRepository(session)
    stub_id = try_uuid(stub_id_or_name)
    record = (
        repository.get_for_update(stub_id, workspace_id=workspace_id)
        if stub_id is not None
        else None
    )
    if record is None:
        record = repository.get_by_name_for_update(stub_id_or_name, workspace_id=workspace_id)
    if record is not None:
        return record
    raise NotFoundError(f"stub not found: {stub_id_or_name}")


def _app_for_update(
    session: Session,
    app_id_or_name: str,
    *,
    workspace_id: str,
    include_deleted: bool,
) -> AppRecord | None:
    repository = AppRepository(session)
    app_id = try_uuid(app_id_or_name)
    if app_id is not None:
        return repository.get_for_update(
            app_id,
            workspace_id=workspace_id,
            include_deleted=include_deleted,
        )
    return repository.get_by_name(
        app_id_or_name,
        workspace_id=workspace_id,
        include_deleted=include_deleted,
        for_update=True,
    )


def _terminal_target_matches(app: AppRecord, target: AppLifecycleTarget) -> bool:
    return (
        (target is AppLifecycleTarget.Active and app.lifecycle_state is AppLifecycleState.Active)
        or (target is AppLifecycleTarget.Paused and app.lifecycle_state is AppLifecycleState.Paused)
        or (
            target is AppLifecycleTarget.Deleted
            and app.lifecycle_state is AppLifecycleState.Deleted
        )
    )


def _app_event_action(target: AppLifecycleTarget) -> str:
    if target is AppLifecycleTarget.Active:
        return "app.resumed"
    if target is AppLifecycleTarget.Paused:
        return "app.paused"
    return "app.deleted"


def _claim_specific(session: Session, app: AppRecord, *, revision: int) -> str | None:
    repository = AppRepository(session)
    current = repository.get_for_update(
        app.id,
        workspace_id=app.workspace_id,
        include_deleted=True,
    )
    if current is None or current.lifecycle_revision != revision:
        return None
    now = utc_now()
    claim_is_live = (
        current.reconcile_claim_id is not None
        and current.reconcile_claimed_at is not None
        and current.reconcile_claimed_at >= now - timedelta(seconds=_RECONCILE_CLAIM_SECONDS)
    )
    if claim_is_live:
        return None
    claim_id = str(uuid4())
    repository.upsert(
        current.model_copy(
            update={
                "reconcile_claim_id": claim_id,
                "reconcile_claimed_at": now,
            }
        )
    )
    return claim_id


__all__ = [
    "AppExecutionLifecycleEffects",
    "AppImageAvailability",
    "AppReader",
    "AppRegistry",
    "AppService",
    "DatabaseAppExecutionAdmission",
    "DatabaseAppImageAvailability",
]
