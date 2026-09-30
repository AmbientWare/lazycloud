"""Opt-in owner benchmark: uv run --group dev pytest -s -q benchmarks/deployment_lifecycle.py.

Uses isolated PostgreSQL/Redis fixtures and the production service composition.
One function has 1, 10, then 100 versions. Each operation warms once, then runs
40 sequential samples. Returned bytes count PostgreSQL field payloads, excluding
protocol framing. This does not measure worker startup or user function execution.
"""

import json
from collections.abc import Callable
from statistics import median, quantiles
from time import perf_counter

from api.server.services import ApiServices
from apps.api.tests.runtime import isolated_services
from database.records.apps import StubRecord
from operations.management import ManagementService
from psycopg import Cursor
from pydantic import JsonValue
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.gateway import DeployStubRequest, GetOrCreateStubRequest
from sqlalchemy import Connection, event
from sqlalchemy.engine.interfaces import ExecutionContext, _DBAPIAnyExecuteParams

__all__ = ["isolated_services", "test_deployment_lifecycle"]


def test_deployment_lifecycle(isolated_services: ApiServices) -> None:
    services = isolated_services
    management = ManagementService(services)
    engine = services.context.database.engine
    totals = [0, 0, 0]

    def record(
        conn: Connection,
        cursor: Cursor[tuple[JsonValue, ...]],
        statement: str,
        parameters: _DBAPIAnyExecuteParams,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        totals[0] += 1
        result = cursor.pgresult
        if result is not None:
            totals[1] += result.ntuples
            totals[2] += sum(
                len(result.get_value(row, column) or b"")
                for row in range(result.ntuples)
                for column in range(result.nfields)
            )

    def measure(name: str, count: int, action: Callable[[], None]) -> None:
        action()
        totals[:] = [0, 0, 0]
        samples: list[float] = []
        event.listen(engine, "after_cursor_execute", record)
        try:
            for _ in range(40):
                started = perf_counter()
                action()
                samples.append((perf_counter() - started) * 1000)
        finally:
            event.remove(engine, "after_cursor_execute", record)
        print(
            "MEASURE "
            + json.dumps(
                {
                    "operation": name,
                    "versions": count,
                    "samples": len(samples),
                    "queries": totals[0] / len(samples),
                    "rows": totals[1] / len(samples),
                    "bytes": totals[2] / len(samples),
                    "p50_ms": median(samples),
                    "p95_ms": quantiles(samples, n=100)[94],
                },
                sort_keys=True,
            )
        )

    created = 0
    stub_id: str | None = None
    for count in (1, 10, 100):
        while created < count:
            deployment = services.deployments.deploy(
                DeploymentSpec(name="measure", kind=DeploymentKind.Function, handler="pkg:run")
            )
            created += 1
            stub_id = deployment.stub_id

        def resolve() -> None:
            services.deployment_resources.resolve_target(
                "measure", DeploymentKind.Function, workspace="default"
            )

        def latest() -> None:
            management.latest_deployments("default")

        def summary() -> None:
            management.app_summaries("default")

        def idle() -> None:
            services.apps.reconcile_pending()

        def idle_deployments() -> None:
            services.deployments.effects.reconcile_pending()

        def release() -> None:
            services.containers.accepting_container_workers({})

        for name, action in (
            ("resolve", resolve),
            ("latest", latest),
            ("summary", summary),
            ("idle_apps", idle),
            ("idle_deployments", idle_deployments),
            ("release_admission", release),
        ):
            measure(name, count, action)

        assert stub_id
        stub = services.control_plane_service.stubs.get_stub(stub_id)

        def invocation_state(stub: StubRecord = stub) -> None:
            with services.context.database.session() as session:
                services.deployment_resources.require_stub_active_in_session(session, stub)

        measure("invocation_state", count, invocation_state)

    def publish() -> None:
        gateway = services.gateway_deployment_service
        prepared = gateway.get_or_create_stub(
            GetOrCreateStubRequest(name="publish", handler="pkg:run", workspace="default")
        )
        gateway.deploy_stub(DeployStubRequest(stub_id=prepared.stub_id, workspace="default"))

    measure("prepare_and_publish", 100, publish)
    deployment_id = services.deployments.get("measure").id

    def stop_start() -> None:
        services.deployments.set_deployment_active("default", deployment_id, active=False)
        services.deployments.set_deployment_active("default", deployment_id, active=True)

    measure("stop_start", 100, stop_start)
