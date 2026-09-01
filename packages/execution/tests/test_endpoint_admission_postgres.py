"""PostgreSQL serializes endpoint admission across API replicas."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.tables.endpoint_dispatch import EndpointDispatchTable
from execution.endpoints.dispatch import (
    EndpointContainerAddress,
    EndpointContainerAddressMap,
    EndpointContainerState,
    EndpointInstanceDispatcher,
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
from tests.service_fixtures import service_graph

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

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


class _NoEndpointContainers:
    def list_by_stub(self, stub_id: str) -> Sequence[EndpointContainerState]:
        del stub_id
        return ()

    def get_container_address(self, container_id: str) -> EndpointContainerAddress | None:
        del container_id
        return None

    def get_container_address_map(self, container_id: str) -> EndpointContainerAddressMap:
        raise AssertionError(f"container has no address: {container_id}")


def test_postgresql_endpoint_admission_holds_one_buffer_slot_across_replicas(
    tmp_path: Path,
) -> None:
    with _postgres_services(tmp_path) as services:
        stub = ControlPlaneService(services.context).create_stub(
            "concurrent-endpoint-admission",
            kind=StubKind.Endpoint,
            handler="module:handler",
            config={
                "runtime": {"image_id": "image", "timeout_seconds": 2},
                "max_pending_tasks": 1,
            },
        )
        start = Barrier(CONTENDERS)

        def invoke() -> EndpointForwardResponse:
            service = EndpointControlService(
                services,
                dispatcher=EndpointInstanceDispatcher(_NoEndpointContainers()),
            )
            start.wait(timeout=30)
            return service.forward_endpoint_request(
                EndpointForwardRequest(stub_id=stub.id, method="POST", body=b"{}")
            )

        with ThreadPoolExecutor(max_workers=CONTENDERS) as executor:
            responses = [
                future.result(timeout=30)
                for future in [executor.submit(invoke) for _ in range(CONTENDERS)]
            ]

        assert [response.status_code for response in responses].count(504) == 1
        assert [response.status_code for response in responses].count(429) == CONTENDERS - 1
        with services.context.database.session() as session:
            dispatch_count = session.scalar(
                select(func.count()).where(EndpointDispatchTable.stub_id == stub.id)
            )
        assert dispatch_count == 1


@contextmanager
def _postgres_services(tmp_path: Path) -> Iterator[ApiServices]:
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
        database = DatabaseClient.from_settings(
            DatabaseSettings(
                url=base_url.set(database=database_name).render_as_string(hide_password=False),
                pool_size=CONTENDERS + 2,
                max_overflow=0,
                application_name=DatabaseApplicationName.Test,
            )
        )
        with service_graph(database, tmp_path) as graph:
            yield replace(
                graph,
                containers=replace(graph.containers, scheduler=_AcceptingScheduler()),
            )
    finally:
        with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()
