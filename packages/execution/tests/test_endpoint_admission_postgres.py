"""PostgreSQL serializes endpoint admission across API replicas."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from billing.rate_publication import publish_metered_rate_history
from control.service import ControlPlaneService, StubKind
from coordination.redis_client import RedisSettings
from database.tables.endpoint_dispatch import EndpointDispatchTable
from execution.endpoints.dispatch import (
    DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    AsyncEndpointResponseStream,
    EndpointContainerState,
    EndpointDispatchTarget,
)
from execution.endpoints.service import EndpointControlService
from shared.http.endpoints import EndpointForwardRequest, EndpointForwardResponse
from shared.scheduling import (
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
    SchedulerWorkerRequest,
)
from sqlalchemy import func, select, text
from tests.backing_services import postgres_url
from tests.real_redis import RealRedisActors
from tests.service_fixtures import service_graph

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
    bootstrap_database,
)

CONTENDERS = 8


class _AcceptingScheduler:
    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        del ready_at
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
        )


class _NoEndpointDispatcher:
    async def select_target(
        self,
        stub_id: str,
        *,
        container_loads: Mapping[str, int] | None = None,
        max_inflight_per_container: int = 1,
        excluded_container_ids: frozenset[str] | set[str] = frozenset(),
    ) -> EndpointDispatchTarget | None:
        del stub_id, container_loads, max_inflight_per_container, excluded_container_ids
        return None

    async def unprobed_target(self, stub_id: str) -> EndpointDispatchTarget | None:
        del stub_id
        return None

    async def container_states(self, stub_id: str) -> Sequence[EndpointContainerState]:
        del stub_id
        return ()

    async def open_backend_socket(self, target: EndpointDispatchTarget) -> socket.socket | None:
        raise AssertionError(f"container has no backend socket: {target.container_id}")

    async def open_http_stream(
        self,
        target: EndpointDispatchTarget,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float = DEFAULT_ENDPOINT_FORWARD_TIMEOUT_SECONDS,
    ) -> AsyncEndpointResponseStream:
        del request, timeout_seconds
        raise AssertionError(f"container has no HTTP stream: {target.container_id}")


@pytest.mark.anyio
async def test_postgresql_endpoint_admission_holds_one_buffer_slot_across_replicas(
    tmp_path: Path,
    real_redis_actors: RealRedisActors,
) -> None:
    async with _postgres_services(tmp_path, real_redis_actors) as services:
        stub = ControlPlaneService(services.context).create_stub(
            "concurrent-endpoint-admission",
            kind=StubKind.Endpoint,
            handler="module:handler",
            config={
                "runtime": {"image_id": "image", "timeout_seconds": 2},
                "max_pending_tasks": 1,
            },
        )
        start = asyncio.Event()

        async def invoke() -> EndpointForwardResponse:
            service = EndpointControlService(
                services,
                async_database=services.require_async_io().database,
                async_dispatcher=_NoEndpointDispatcher(),
            )
            await start.wait()
            return await service.forward_endpoint_request(
                EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
            )

        invocations = [asyncio.create_task(invoke()) for _ in range(CONTENDERS)]
        start.set()
        responses = await asyncio.gather(*invocations)

        assert [response.status_code for response in responses].count(504) == 1
        assert [response.status_code for response in responses].count(429) == CONTENDERS - 1
        with services.context.database.session() as session:
            dispatch_count = session.scalar(
                select(func.count()).where(EndpointDispatchTable.stub_id == stub.id)
            )
        assert dispatch_count == 1


@asynccontextmanager
async def _postgres_services(
    tmp_path: Path,
    real_redis_actors: RealRedisActors,
) -> AsyncIterator[ApiServices]:
    base_url = postgres_url()
    database_name = f"endpoint_admission_{uuid4().hex}"
    admin = DatabaseClient.from_settings(
        DatabaseSettings(
            url=base_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    try:
        with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        database_settings = DatabaseSettings(
            url=base_url.set(database=database_name).render_as_string(hide_password=False),
            pool_size=CONTENDERS + 2,
            max_overflow=0,
            application_name=DatabaseApplicationName.Test,
        )
        bootstrap_database(database_settings.url)
        database = DatabaseClient.from_settings(database_settings)
        async_io = ApiAsyncIo.from_settings(
            database_settings,
            RedisSettings(
                url=real_redis_actors.url,
                key_prefix=real_redis_actors.prefix,
                socket_timeout_seconds=2.0,
                health_check_interval_seconds=1,
            ),
        )
        try:
            with database.session() as session:
                publish_metered_rate_history(session)
            with service_graph(
                database,
                tmp_path,
                redis_client=real_redis_actors.client(),
                binary_redis_client=real_redis_actors.client(decode_responses=False),
                async_io=async_io,
            ) as graph:
                yield replace(
                    graph,
                    containers=replace(graph.containers, scheduler=_AcceptingScheduler()),
                )
        finally:
            await async_io.close()
    finally:
        with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()
