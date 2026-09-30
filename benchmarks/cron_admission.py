"""Measure cron admission over real PostgreSQL and Redis, without worker execution.

Run with uv run --group dev pytest -s -q benchmarks/cron_admission.py.
Each workload has 12 measured ticks after one warmup. SQL bytes exclude framing.
"""

import json
from datetime import timedelta
from statistics import median, quantiles
from time import perf_counter

from api.fastapi_app import create_app
from api.server.services import ApiServices
from apps.api.tests.runtime import isolated_services
from control.service import ControlServices
from execution.functions.service import FunctionControlService
from fastapi.testclient import TestClient
from psycopg import Cursor
from pydantic import JsonValue
from scheduler.cron import CronScheduler
from shared.deployment_records import DeploymentSpec
from shared.timestamps import utc_now
from sqlalchemy import Connection, event, text
from sqlalchemy.engine.interfaces import ExecutionContext, _DBAPIAnyExecuteParams
from tests.workspaces import administrator_credential, owned_workspace

__all__ = ["isolated_services", "test_cron_admission"]


def test_cron_admission(isolated_services: ApiServices) -> None:
    services = isolated_services
    scheduler = CronScheduler(services.cron_jobs, FunctionControlService(services))
    totals = [0, 0]

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
            totals[1] += sum(
                len(result.get_value(row, column) or b"")
                for row in range(result.ntuples)
                for column in range(result.nfields)
            )

    now = utc_now()
    created = 0
    for count in (0, 1, 10, 100):
        while created < count:
            workspace = f"cron-benchmark-{created // 10}"
            if created % 10 == 0:
                owned_workspace(ControlServices.create(services.context), workspace)
            services.deployments.deploy(
                DeploymentSpec(
                    name=f"cron-benchmark-{created}", handler="pkg:run", cron="every 1m"
                ),
                workspace=workspace,
            )
            created += 1
        now += timedelta(minutes=1)
        scheduler.tick(now=now, limit=100)
        totals[:] = [0, 0]
        samples: list[float] = []
        event.listen(services.database.engine, "after_cursor_execute", record)
        try:
            for _ in range(12):
                now += timedelta(minutes=1)
                started = perf_counter()
                runs = scheduler.tick(now=now, limit=100)
                samples.append((perf_counter() - started) * 1000)
                assert len(runs) == count
                assert all(run.enqueued for run in runs), [
                    run.reason for run in runs if not run.enqueued
                ]
        finally:
            event.remove(services.database.engine, "after_cursor_execute", record)
        print(
            "MEASURE "
            + json.dumps(
                {
                    "due_jobs": count,
                    "samples": len(samples),
                    "queries": totals[0] / len(samples),
                    "bytes": totals[1] / len(samples),
                    "p50_ms": median(samples),
                    "p95_ms": quantiles(samples, n=100)[94],
                }
            )
        )


def test_cron_history_reads(isolated_services: ApiServices) -> None:
    services = isolated_services
    for index in range(100):
        services.deployments.deploy(
            DeploymentSpec(name=f"history-{index}", handler="pkg:run", cron="@daily")
        )
    target = services.cron_jobs.list()[0]
    with services.database.session() as session:
        session.execute(
            text(
                "INSERT INTO cron_job_runs (id, workspace_id, cron_job, enqueued, created_at) "
                "SELECT gen_random_uuid(), :workspace_id, 'retained', false, "
                "now() - interval '7 days' FROM generate_series(1, 10000)"
            ),
            {"workspace_id": target.workspace_id},
        )
    token, _ = administrator_credential(services.context)
    totals = [0, 0]

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
            totals[1] += sum(
                len(result.get_value(row, column) or b"")
                for row in range(result.ntuples)
                for column in range(result.nfields)
            )

    scheduler = CronScheduler(services.cron_jobs, FunctionControlService(services))
    with TestClient(create_app(services)) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        for operation in ("idle_with_history", "workload_schedule", "run_history"):
            samples: list[float] = []
            totals[:] = [0, 0]
            event.listen(services.database.engine, "after_cursor_execute", record)
            try:
                for _ in range(40):
                    started = perf_counter()
                    if operation == "idle_with_history":
                        assert scheduler.tick() == []
                    else:
                        path = (
                            "/api/v1/cron-jobs"
                            if operation == "workload_schedule"
                            else "/api/v1/cron-job-runs"
                        )
                        response = client.get(
                            path,
                            params={
                                "workspace": target.workspace_id,
                                "deployment_id": target.deployment_id,
                                "limit": 10,
                            },
                        )
                        assert response.status_code == 200
                    samples.append((perf_counter() - started) * 1000)
            finally:
                event.remove(services.database.engine, "after_cursor_execute", record)
            print(
                "MEASURE "
                + json.dumps(
                    {
                        "operation": operation,
                        "schedules": 100,
                        "history": 10000,
                        "samples": len(samples),
                        "queries": totals[0] / len(samples),
                        "bytes": totals[1] / len(samples),
                        "p50_ms": median(samples),
                        "p95_ms": quantiles(samples, n=100)[94],
                    }
                )
            )
