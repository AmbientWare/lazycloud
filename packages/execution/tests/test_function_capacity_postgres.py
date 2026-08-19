"""A function's container ceiling holds when the starts arrive together.

The overshoot this covers was not a wrong limit, it was a limit read outside the
transaction that acted on it: six invocations arriving at once each counted the
same zero containers and each started one, and the stub finished holding nine
against a ceiling of six. Only a real PostgreSQL backend can show that — SQLite
admits one writer at a time, so every burst against it serializes for free and
an unlocked read looks correct.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.tables.orchestration import ContainerTable
from execution.functions.service import FunctionControlService
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import FunctionInvokeBody
from shared.scheduling import (
    SchedulerContainerSubmitResult,
    SchedulerContainerSubmitStatus,
    SchedulerWorkerRequest,
)
from sqlalchemy import func, select, text
from tests.backing_services import postgres_url
from tests.service_fixtures import service_graph

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

CEILING = 3
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


def test_postgresql_function_capacity_is_bounded_when_starts_race(tmp_path: Path) -> None:
    with _postgres_services(tmp_path) as services:
        _prove_a_simultaneous_burst_starts_one_container(services)
        _prove_the_autoscaler_stops_at_the_ceiling(services)


def _prove_a_simultaneous_burst_starts_one_container(services: ApiServices) -> None:
    """Every invocation of an idle stub wants a container; one of them gets it.

    The invoke path may bring a stub up from nothing so a single cold call is
    not made to wait for a scheduler tick, and that is all it may do — depth is
    a judgement about a backlog, and only the autoscaler sees one whole. Without
    the reservation holding the count it decides on, each of these reads zero
    and this is a container per call, which is the arrangement pooling replaced.
    """

    functions = FunctionControlService(services)
    stub = ControlPlaneService(services.context).create_stub(
        "capacity-burst",
        kind=StubKind.Function,
        handler="module:handler",
        config={"runtime": {"image_id": "image"}, "autoscaler": {"max_containers": CEILING}},
    )
    start = Barrier(CONTENDERS)

    def invoke(value: int) -> str:
        start.wait(timeout=30)
        return functions.function_invoke(
            FunctionInvokeBody(
                stub_id=stub.id,
                invocation=FunctionJsonInvocation(args=[value]),
            )
        ).task_id

    with ThreadPoolExecutor(max_workers=CONTENDERS) as executor:
        task_ids = [
            future.result(timeout=60)
            for future in [executor.submit(invoke, value) for value in range(CONTENDERS)]
        ]

    assert len(set(task_ids)) == CONTENDERS
    assert _container_count(services, stub_id=stub.id) == 1


def _prove_the_autoscaler_stops_at_the_ceiling(services: ApiServices) -> None:
    """Concurrent scale-ups provision the backlog and then refuse.

    The autoscaler asks for one container at a time and reads the count between
    asks, so a ceiling honoured only in its own loop would still hold here. What
    is proved is the refusal itself: past the ceiling the reservation declines,
    whichever caller holds the authority to ask.
    """

    functions = FunctionControlService(services)
    stub = ControlPlaneService(services.context).create_stub(
        "capacity-autoscaled",
        kind=StubKind.Function,
        handler="module:handler",
        config={"runtime": {"image_id": "image"}, "autoscaler": {"max_containers": CEILING}},
    )
    for value in range(CONTENDERS):
        functions.function_invoke(
            FunctionInvokeBody(
                stub_id=stub.id,
                invocation=FunctionJsonInvocation(args=[value]),
            )
        )
    start = Barrier(CONTENDERS)

    def scale() -> bool:
        start.wait(timeout=30)
        return functions.start_function_container(stub.id)

    with ThreadPoolExecutor(max_workers=CONTENDERS) as executor:
        started = [
            future.result(timeout=60)
            for future in [executor.submit(scale) for _ in range(CONTENDERS)]
        ]

    assert _container_count(services, stub_id=stub.id) == CEILING
    # One was already up from the invocation that found the stub idle, so the
    # ceiling leaves room for two more and the rest are refused.
    assert sum(started) == CEILING - 1


def _container_count(services: ApiServices, *, stub_id: str) -> int:
    with services.context.database.session() as session:
        count = session.scalar(select(func.count()).where(ContainerTable.stub_id == stub_id))
    assert count is not None
    return count


@contextmanager
def _postgres_services(tmp_path: Path) -> Iterator[ApiServices]:
    base_url = postgres_url()
    database_name = f"function_capacity_{uuid4().hex}"
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
                # Every contender holds a session while it waits on the capacity
                # lock, so a pool shallower than the burst would serialize them
                # in the pool and prove nothing about the lock.
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
