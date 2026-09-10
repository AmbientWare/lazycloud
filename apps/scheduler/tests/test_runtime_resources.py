from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Never

import pytest
from agent.binary import AgentBinarySettings
from compute.reclaim import ComputeReclaimPolicy
from gateway.settings import GatewaySettings
from images.settings import ImageBuildContainerSettings
from networking.settings import BackendRouteSettings
from observability.settings import (
    VolumeMeteringSettings,
    WorkspaceChangeStreamSettings,
)
from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings
from scheduler.service import DEFAULT_AUTOSCALING_RECONCILE_LIMIT
from scheduler.state import RedisSchedulerContainerRepository
from scheduler_app import main as scheduler
from scheduler_app.runtime import SchedulerRuntime
from scheduler_app.services import (
    SchedulerCapacitySettings,
    SchedulerNetworkSettings,
    SchedulerObservabilitySettings,
    SchedulerStorageSettings,
)
from shared.containers import ContainerStatus
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from storage.image_archive import ImageArchiveSettings
from storage.retention_settings import RetentionSettings
from storage_client.s3 import S3ObjectStoreSettings
from tests.real_redis import RealRedisActors


@pytest.fixture
def scheduler_runtime(
    seeded_database_url: URL,
    real_redis_actors: RealRedisActors,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[SchedulerRuntime]:
    dsn = seeded_database_url.render_as_string(hide_password=False)
    monkeypatch.setenv("LAZYCLOUD_DATABASE_URL", dsn)
    monkeypatch.setenv("LAZYCLOUD_DATABASE_DIRECT_URL", dsn)
    monkeypatch.setenv("LAZYCLOUD_REDIS_URL", real_redis_actors.url)
    monkeypatch.setenv("LAZYCLOUD_REDIS_KEY_PREFIX", real_redis_actors.prefix)
    gateway = GatewaySettings()
    objects = S3ObjectStoreSettings()
    runtime = SchedulerRuntime.create(
        public_gateway_http_url=gateway.public_http_url,
        runtime_callback_http_url=gateway.runtime_callback_http_url,
        observability=SchedulerObservabilitySettings(
            workspace_changes=WorkspaceChangeStreamSettings()
        ),
        storage=SchedulerStorageSettings(
            object_store=objects,
            image_archive=ImageArchiveSettings(bucket=objects.bucket),
            retention=RetentionSettings(),
            volume_metering=VolumeMeteringSettings(),
        ),
        network=SchedulerNetworkSettings(backend_routes=BackendRouteSettings()),
        capacity=SchedulerCapacitySettings(
            aws_connections=AwsAccountConnectionSettings(),
            aws_capacity=AwsCapacitySettings(),
            agent_binaries=AgentBinarySettings(),
            reclaim=ComputeReclaimPolicy(),
        ),
        image_build_container_settings=ImageBuildContainerSettings(),
    )
    try:
        yield runtime
    finally:
        runtime.close()


def test_scheduler_runtime_closes_owned_services_on_exception(
    scheduler_runtime: SchedulerRuntime,
    monkeypatch: pytest.MonkeyPatch,
    postgres_admin: Engine,
) -> None:
    runtime = scheduler_runtime
    app_services = runtime.owned_services
    assert app_services is not None
    with app_services.context.database.session() as session:
        backend_pid = session.scalar(text("SELECT pg_backend_pid()"))
    with postgres_admin.connect() as connection:
        assert connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = :pid)"),
            {"pid": backend_pid},
        )

    def fail_run_once(
        *,
        now: datetime | None = None,
        include_cron_jobs: bool = True,
        include_containers: bool = True,
        include_container_dispatch: bool = True,
        container_limit: int = 100,
        autoscaling_limit: int = DEFAULT_AUTOSCALING_RECONCILE_LIMIT,
    ) -> Never:
        _ = (
            now,
            include_cron_jobs,
            include_containers,
            include_container_dispatch,
            container_limit,
            autoscaling_limit,
        )
        raise RuntimeError("scheduler pass failed")

    monkeypatch.setattr(runtime.scheduler, "run_once", fail_run_once)

    with pytest.raises(RuntimeError, match="scheduler pass failed"):
        scheduler.run_scheduler(runtime=runtime, once=True)

    with postgres_admin.connect() as connection:
        assert not connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = :pid)"),
            {"pid": backend_pid},
        )


def test_scheduler_app_services_stop_cancels_the_real_scheduler_request(
    scheduler_runtime: SchedulerRuntime,
    real_redis_actors: RealRedisActors,
) -> None:
    app_services = scheduler_runtime.owned_services
    assert app_services is not None
    container_state = RedisSchedulerContainerRepository(real_redis_actors.client())

    container = app_services.containers.run(
        "stale-sandbox",
        "python:3.12-slim",
        ["sleep", "30"],
        docker_enabled=True,
    )

    stopped = app_services.containers.stop(container.id)

    assert stopped.status is ContainerStatus.Stopped
    assert container_state.is_container_cancelled(container.id)
