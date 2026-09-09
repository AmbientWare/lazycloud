from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from threading import Event

import pytest
from api.server.services import ApiServices
from control.apps import DatabaseAppExecutionAdmission
from database.records.apps import AppRecord
from database.tables.execution import TaskTable
from database.tables.orchestration import ContainerTable
from database.types import DatabaseSession
from shared.app_identity import FUNCTION_IMAGE
from shared.app_lifecycle import AppLifecycleState
from shared.container_requests import ContainerShutdownTarget
from shared.errors import ConflictError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from control import apps as apps_module


@dataclass(slots=True)
class _RecordingExecutionEffects:
    stopped: list[list[ContainerShutdownTarget]] = field(default_factory=list)

    def stop_app_containers(
        self,
        *,
        workspace_id: str,
        app_id: str,
        container_targets: list[ContainerShutdownTarget],
    ) -> None:
        del workspace_id, app_id
        self.stopped.append(container_targets)

    def delete_app_execution(self, *, workspace_id: str, app_id: str) -> None:
        del workspace_id, app_id


@dataclass(frozen=True, slots=True)
class _BlockingExecutionAdmission:
    locked: Event
    release: Event
    inner: DatabaseAppExecutionAdmission = field(default_factory=DatabaseAppExecutionAdmission)

    def assert_active(
        self,
        session: DatabaseSession,
        *,
        app_id: str,
        workspace_id: str,
    ) -> None:
        self.inner.assert_active(session, app_id=app_id, workspace_id=workspace_id)
        self.locked.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test did not release app execution admission")


def test_postgresql_app_execution_admission_serializes_container_creation_and_pause(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prove_creation_that_holds_the_lock_is_captured(isolated_services)
    _prove_creation_after_lifecycle_begin_is_rejected_without_orphans(
        isolated_services,
        monkeypatch=monkeypatch,
    )


def _prove_creation_that_holds_the_lock_is_captured(services: ApiServices) -> None:
    app = services.apps.create("creation_first")
    effects = _RecordingExecutionEffects()
    lifecycle = replace(services.apps, execution_effects=effects)
    admission_locked = Event()
    release_admission = Event()
    containers = replace(
        services.containers,
        app_admission=_BlockingExecutionAdmission(admission_locked, release_admission),
    )
    pause_started = Event()

    def pause() -> AppRecord:
        pause_started.set()
        return lifecycle.pause(app.id, workspace=app.workspace_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        creation = executor.submit(
            containers.run,
            "creation-first-container",
            FUNCTION_IMAGE,
            ["python", "-m", "runner.function"],
            workspace_id=app.workspace_id,
            app_id=app.id,
        )
        assert admission_locked.wait(timeout=10)
        pause_operation = executor.submit(pause)
        assert pause_started.wait(timeout=10)
        assert not pause_operation.done()
        release_admission.set()
        container = creation.result(timeout=10)
        paused = pause_operation.result(timeout=10)

    assert paused.lifecycle_state is AppLifecycleState.Paused
    assert [[target.container_id for target in batch] for batch in effects.stopped] == [
        [container.id]
    ]


def _prove_creation_after_lifecycle_begin_is_rejected_without_orphans(
    services: ApiServices,
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = services.apps.create("pause_first")
    effects = _RecordingExecutionEffects()
    lifecycle = replace(services.apps, execution_effects=effects)
    lifecycle_locked = Event()
    release_lifecycle = Event()
    creation_started = Event()
    original_claim = apps_module._claim_specific

    def blocking_claim(session: Session, current: AppRecord, *, revision: int) -> str | None:
        claim_id = original_claim(session, current, revision=revision)
        lifecycle_locked.set()
        if not release_lifecycle.wait(timeout=10):
            raise TimeoutError("test did not release app lifecycle begin")
        return claim_id

    monkeypatch.setattr(apps_module, "_claim_specific", blocking_claim)

    def create_container() -> None:
        creation_started.set()
        services.containers.run(
            "pause-first-container",
            FUNCTION_IMAGE,
            ["python", "-m", "runner.function"],
            workspace_id=app.workspace_id,
            app_id=app.id,
        )

    before = _execution_row_counts(services, app_id=app.id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pause_operation = executor.submit(lifecycle.pause, app.id, workspace=app.workspace_id)
        assert lifecycle_locked.wait(timeout=10)
        creation = executor.submit(create_container)
        assert creation_started.wait(timeout=10)
        assert not creation.done()
        release_lifecycle.set()
        paused = pause_operation.result(timeout=10)
        with pytest.raises(ConflictError, match="is not active"):
            creation.result(timeout=10)

    assert paused.lifecycle_state is AppLifecycleState.Paused
    assert effects.stopped == [[]]
    assert _execution_row_counts(services, app_id=app.id) == before


def _execution_row_counts(services: ApiServices, *, app_id: str) -> tuple[int, int]:
    with services.context.database.session() as session:
        task_count = session.scalar(select(func.count()).where(TaskTable.app_id == app_id))
        container_count = session.scalar(
            select(func.count()).where(ContainerTable.app_id == app_id)
        )
    assert task_count is not None
    assert container_count is not None
    return task_count, container_count
