from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from api.server.async_io import ApiAsyncIo
from billing.rate_publication import publish_metered_rate_history
from coordination.redis_client import RedisSettings
from database.migrations import bootstrap_database
from database.repositories.image_build_dispatch import ImageBuildDispatchRepository
from scheduler.containers import SchedulerContainerDispatchStatus
from shared.containers import ContainerStatus
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import BuildStatus
from shared.timestamps import utc_now
from sqlalchemy import create_engine, text
from tests.backing_services import postgres_url
from tests.real_redis import RealRedisActors
from tests.service_fixtures import service_graph

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_scheduling_failure_finishes_image_build_stream_and_cleans_execution(
    tmp_path: Path, real_redis_actors: RealRedisActors
) -> None:
    base_url = postgres_url()
    database_name = f"lazycloud_image_failure_{uuid4().hex}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin = create_engine(
        base_url, connect_args={"application_name": DatabaseApplicationName.Test.value}
    )
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            identifier = connection.dialect.identifier_preparer.quote_identifier(database_name)
            connection.execute(text(f"CREATE DATABASE {identifier}"))
        bootstrap_database(database_url)
        settings = DatabaseSettings(url=database_url, application_name=DatabaseApplicationName.Test)
        database = DatabaseClient.from_settings(settings)
        async_io = ApiAsyncIo.from_settings(
            settings,
            RedisSettings(url=real_redis_actors.url, key_prefix=real_redis_actors.prefix),
        )
        with database.session() as session:
            publish_metered_rate_history(session)
        with service_graph(
            database,
            tmp_path,
            redis_client=real_redis_actors.client(),
            binary_redis_client=real_redis_actors.client(decode_responses=False),
            async_io=async_io,
        ) as services:
            with database.session() as session:
                workspace_id = services.context.default_workspace_id(session)
            scheduler = services.scheduler_container_requests
            scheduler.max_retry_count = 0
            scheduler.retry_grace_seconds = 0
            build = services.images.build(
                ImageSpec(base="scratch", ignore_python=True), workspace_id=workspace_id
            )
            [failure] = scheduler.dispatch_ready(limit=1)

            assert failure.status is SchedulerContainerDispatchStatus.Failed
            failed = services.images.get(build.id, workspace_id=workspace_id)
            assert failed.status is BuildStatus.Failed
            assert failed.error == failure.reason
            assert failed.started_at is None
            assert failed.finished_at is not None
            assert services.containers.get(build.id).status is ContainerStatus.Failed
            events = list(services.image_service.follow_build(build.id, workspace_id=workspace_id))
            assert events[-1].response.done
            assert not events[-1].response.success
            assert events[-1].response.error == failure.reason
            with database.session() as session:
                dispatch = ImageBuildDispatchRepository(session)
                assert dispatch.payload(build.id, workspace_id=workspace_id) is None
                assert dispatch.cleanup_due(now=utc_now(), limit=1) == [(build.id, workspace_id)]
            services.images.submission.cleanup(build_id=build.id, limit=1)
            with database.session() as session:
                assert (
                    ImageBuildDispatchRepository(session).cleanup_due(now=utc_now(), limit=1) == []
                )
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            identifier = connection.dialect.identifier_preparer.quote_identifier(database_name)
            connection.execute(text(f"DROP DATABASE IF EXISTS {identifier} WITH (FORCE)"))
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM pg_database WHERE datname = :database_name"),
                    {"database_name": database_name},
                )
                == 0
            )
        admin.dispose()
