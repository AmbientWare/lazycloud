from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.apps import DatabaseAppExecutionAdmission
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from database.records.apps import AppRecord
from database.tables.execution import TaskTable
from database.tables.orchestration import ContainerTable
from database.types import DatabaseSession
from shared.app_identity import FUNCTION_IMAGE
from shared.app_lifecycle import AppLifecycleState
from shared.container_requests import ContainerShutdownTarget
from shared.errors import ConflictError
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from storage.volume_filesystem import LocalVolumeFilesystem
from tests.redis_fakes import FakeRedis

from control import apps as apps_module
from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _postgres_services(tmp_path) as services:
        _prove_creation_that_holds_the_lock_is_captured(services)
        _prove_creation_after_lifecycle_begin_is_rejected_without_orphans(
            services,
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


@contextmanager
def _postgres_services(tmp_path: Path) -> Iterator[ApiServices]:
    base_url_value = os.getenv("LAZYCLOUD_TEST_DATABASE_URL")
    if base_url_value is None:
        pytest.skip("LAZYCLOUD_TEST_DATABASE_URL is not configured")
    base_url = make_url(base_url_value)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("LAZYCLOUD_TEST_DATABASE_URL is not PostgreSQL")
    database_name = f"app_admission_{uuid4().hex}"
    database_url = base_url.set(database=database_name)
    admin = DatabaseClient.from_settings(
        DatabaseSettings(
            url=base_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    services: ApiServices | None = None
    try:
        with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        database = DatabaseClient.from_settings(
            DatabaseSettings(
                url=database_url.render_as_string(hide_password=False),
                pool_size=6,
                max_overflow=0,
                application_name=DatabaseApplicationName.Test,
            )
        )
        redis = RedisClient(FakeRedis(), key_prefix=f"app-admission-{uuid4()}")
        services = ApiServices.create(
            database,
            root=tmp_path,
            redis_client=redis,
            binary_redis_client=redis.with_key_prefix("binary"),
            owns_redis_client=False,
            owns_binary_redis_client=False,
            volume_filesystem=LocalVolumeFilesystem(tmp_path / "volumes"),
        )
        ControlPlaneService(services.context).upsert_workspace("default")
        yield services
    finally:
        if services is not None:
            services.close()
        with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()
