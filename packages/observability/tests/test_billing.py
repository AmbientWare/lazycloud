from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import ExitStack
from csv import DictReader
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from io import StringIO
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from cli.main import build_admin_cli
from compute.agent_control import hash_compute_token
from compute.state import ComputeAgentTokenState, RedisComputeStateRepository
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from database.records.apps import AppRecord, StubRecord
from database.repositories.apps import AppRepository, StubRepository
from database.repositories.compute import (
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
)
from database.repositories.observability import UsageRepository
from database.repositories.usage_billing import UsageBillingRepository
from fastapi.testclient import TestClient
from gateway.http import AgentMetricSnapshot, AgentTelemetryRequest
from identity.auth import AuthService
from observability.settings import UsagePricingSettings
from pydantic import JsonValue, TypeAdapter, ValidationError
from shared.billing import BillableMetric, BillingCoverageStatus
from shared.compute_enrollment import MachineReadinessPhase
from shared.compute_policy import MachinePool, UnitName
from shared.deployments import StubKind
from shared.gpu import SUPPORTED_GPU_TYPES
from shared.http.usage import UsageBillingPeriod
from shared.http_transport import HttpChannel
from shared.timestamps import utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageBillingOwner,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from shared.usage_query import UsageQuery
from tests.redis_fakes import FakeRedis
from tests.service_fixtures import (
    administrator_credential,
    owned_workspace,
    workspace_owner_user_id,
)

cli = build_admin_cli()

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _TestClientHttpChannel(HttpChannel):
    def __init__(self, client: TestClient, *, token: str) -> None:
        super().__init__(token=token)
        self._client = client

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue:
        headers = {"Authorization": f"Bearer {self.token}"}
        response = self._client.request(
            method,
            path,
            headers=headers,
            json=dict(payload) if payload is not None else None,
        )
        assert response.status_code < 400, response.text
        if response.status_code == 204:
            return None
        return _JSON_VALUE_ADAPTER.validate_python(response.json())


def test_task_count_usage_is_owner_scoped_and_idempotent(
    isolated_services: ApiServices,
) -> None:
    other_workspace = owned_workspace(ControlPlaneService(isolated_services.context), "external")
    with isolated_services.context.database.session() as session:
        owner_workspace_id = isolated_services.context.default_workspace_id(session)
        other_workspace_id = other_workspace.id

    task = isolated_services.tasks.create(
        "owner-billed-task",
        workspace_id=owner_workspace_id,
    )

    isolated_services.usage.record_task_count(
        workspace_id=owner_workspace_id,
        resource_type="function",
        resource_id="stub-public",
        task_id=task.id,
        kind="function",
        app_id="app-public",
        deployment_id="deployment-public",
    )
    isolated_services.usage.record_task_count(
        workspace_id=owner_workspace_id,
        resource_type="function",
        resource_id="stub-public",
        task_id=task.id,
        kind="function",
        app_id="app-public",
        deployment_id="deployment-public",
    )

    owner_summary = isolated_services.usage.aggregate(
        query=UsageQuery(workspace_id=owner_workspace_id)
    )
    external_summary = isolated_services.usage.aggregate(
        query=UsageQuery(workspace_id=other_workspace_id)
    )
    owner_by_metric = {row.metric: row.quantity for row in owner_summary}
    [record] = isolated_services.usage.list(
        UsageQuery(workspace_id=owner_workspace_id, resource_id="stub-public")
    )

    assert owner_by_metric[UsageMetric.TaskCount] == 1
    assert record.labels == {
        "kind": "function",
        "stub_id": "stub-public",
        "app_id": "app-public",
        "deployment_id": "deployment-public",
    }
    assert external_summary == []


def test_usage_record_api_pages_more_than_one_thousand_records(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        usage = UsageRepository(session)
        for index in range(1005):
            usage.record(
                workspace_id=workspace_id,
                resource_type="container",
                resource_id=f"container-{index}",
                metric=UsageMetric.DiskWriteBytes,
                quantity=index + 1,
                unit=UsageUnit.Bytes,
            )

    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = _auth_headers(isolated_services)
    cursor = ""
    record_ids: set[str] = set()
    page_count = 0
    while True:
        params: dict[str, str | int] = {
            "metric": UsageMetric.DiskWriteBytes.value,
            "limit": 400,
        }
        if cursor:
            params["cursor"] = cursor
        response = client.get("/api/v1/usage/records", params=params, headers=headers)
        assert response.status_code == 200
        payload = response.json()
        page_ids = {record["id"] for record in payload["data"]}
        assert not record_ids & page_ids
        record_ids.update(page_ids)
        page_count += 1
        cursor = payload["next"]
        if not cursor:
            break

    assert page_count == 3
    assert len(record_ids) == 1005


def test_billing_prefers_direct_compute_only_within_the_same_metering_window(
    isolated_services: ApiServices,
) -> None:
    report_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    labels = {
        "cpu_millicores": "1000",
        "mem_mb": "1024",
        "gpu_count": "0",
        "worker_id": "worker-windowed",
    }
    records = (
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-windowed",
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=1_000,
            unit=UsageUnit.Milliseconds,
            labels=labels,
            metadata={"worker_id": "worker-windowed", "window_start_ms": 0, "window_end_ms": 1000},
            created_at=report_start + timedelta(minutes=10),
        ),
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-windowed",
            metric=UsageMetric.CpuSeconds,
            quantity=2,
            unit=UsageUnit.Seconds,
            labels=labels,
            metadata={"worker_id": "worker-windowed", "window_start_ms": 0, "window_end_ms": 1000},
            created_at=report_start + timedelta(minutes=10, seconds=2),
        ),
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-windowed",
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=2_000,
            unit=UsageUnit.Milliseconds,
            labels=labels,
            metadata={
                "worker_id": "worker-windowed",
                "window_start_ms": 1000,
                "window_end_ms": 3000,
            },
            created_at=report_start + timedelta(minutes=20),
        ),
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-windowed",
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=3_000,
            unit=UsageUnit.Milliseconds,
            labels=labels,
            created_at=report_start + timedelta(minutes=30),
        ),
    )
    for record in records:
        isolated_services.usage.append(record)

    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=report_start,
        end=report_start + timedelta(hours=2),
        bucket_seconds=3600,
    )
    overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=report_start,
        end=report_start + timedelta(hours=2),
        bucket_seconds=3600,
    )

    lines = {line.metric: line for line in report.summary}
    assert lines[BillableMetric.CpuSeconds].quantity == 7
    assert lines[BillableMetric.MemoryGibSeconds].quantity == 6
    assert overview.summary == report.summary
    assert overview.activity == report.activity


def test_gpu_is_priced_per_model_and_the_rollup_agrees_with_the_report(
    isolated_services: ApiServices,
) -> None:
    """Each GPU model bills at its own rate, and both billing paths agree.

    The report reads raw usage; the overview reads rolled-up windows. They are
    separate queries over separate tables, so a model carried by one and not the
    other is two different bills for one month.
    """

    report_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    def gpu_record(container: str, gpu: str, milliseconds: int) -> UsageRecord:
        return UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id=container,
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=milliseconds,
            unit=UsageUnit.Milliseconds,
            labels={
                "cpu_millicores": "1000",
                "mem_mb": "1024",
                "gpu": gpu,
                "gpu_count": "1",
                "worker_id": f"worker-{gpu.lower()}",
            },
            created_at=report_start + timedelta(minutes=10),
        )

    for record in (
        gpu_record("container-h100", "H100", 10_000),
        gpu_record("container-t4", "T4", 10_000),
    ):
        isolated_services.usage.append(record)

    report_end = report_start + timedelta(hours=2)
    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id, start=report_start, end=report_end, bucket_seconds=3600
    )
    overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id, start=report_start, end=report_end, bucket_seconds=3600
    )

    gpu_lines = {
        line.variant: line for line in report.summary if line.metric is BillableMetric.GpuSeconds
    }

    assert set(gpu_lines) == {"H100", "T4"}
    # Ten seconds on each chip, charged at each chip's own rate.
    assert gpu_lines["H100"].quantity == 10
    assert gpu_lines["T4"].quantity == 10
    for line in gpu_lines.values():
        assert line.price_per_unit_nanos is not None
        assert line.cost_nanos == line.quantity * line.price_per_unit_nanos
    assert gpu_lines["H100"].cost_nanos > gpu_lines["T4"].cost_nanos
    assert overview.summary == report.summary


def test_a_gpu_with_no_rate_still_reports_its_seconds(
    isolated_services: ApiServices,
) -> None:
    """Usage nobody priced appears on the bill at zero, never as nothing.

    Dropping the key would drop the quantity with it, billing nothing for compute
    that plainly ran and leaving no line to say so.
    """

    report_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    isolated_services.usage.record(
        workspace_id=workspace_id,
        resource_type="container",
        resource_id="container-unpriced-gpu",
        metric=UsageMetric.ContainerDurationMilliseconds,
        quantity=10_000,
        unit=UsageUnit.Milliseconds,
        # V100 is a real GpuType the sell-price catalog does not carry.
        labels={"cpu_millicores": "0", "mem_mb": "0", "gpu": "V100", "gpu_count": "1"},
    )

    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=report_start,
        end=report_start + timedelta(hours=2),
        bucket_seconds=3600,
    )

    gpu_lines = [line for line in report.summary if line.metric is BillableMetric.GpuSeconds]

    assert len(gpu_lines) == 1
    assert gpu_lines[0].variant == "V100"
    assert gpu_lines[0].quantity == 10
    assert gpu_lines[0].price_per_unit_nanos is None
    assert gpu_lines[0].cost_nanos == 0
    # GPU is the only metric with usage here and it has no rate, so the whole
    # period is unpriced rather than partly priced.
    assert report.coverage.status is BillingCoverageStatus.Unpriced
    assert any("V100" in gap.reason for gap in report.coverage.gaps)


def test_a_label_disagreement_inside_one_window_cannot_double_bill(
    isolated_services: ApiServices,
) -> None:
    """Both billing paths agree even when records in one window disagree on GPU.

    The rollup keys a window by GPU model; the report does not. A window whose
    duration record names a model while its CPU record does not therefore splits
    into two rollup rows — and direct-beats-derived is applied per row, so the
    measured CPU seconds and the reserved CPU seconds are both counted.
    """

    report_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    window: dict[str, JsonValue] = {
        "worker_id": "worker-split",
        "window_start_ms": 0,
        "window_end_ms": 10_000,
        METERING_WINDOW_STARTED_AT_METADATA_KEY: report_start.isoformat(),
        METERING_WINDOW_ENDED_AT_METADATA_KEY: (report_start + timedelta(seconds=10)).isoformat(),
    }
    common = {"cpu_millicores": "1000", "mem_mb": "1024", "worker_id": "worker-split"}
    isolated_services.usage.append(
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-split",
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=10_000,
            unit=UsageUnit.Milliseconds,
            labels={**common, "gpu": "H100", "gpu_count": "1"},
            metadata=window,
            created_at=report_start + timedelta(minutes=5),
        )
    )
    isolated_services.usage.append(
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-split",
            metric=UsageMetric.CpuSeconds,
            quantity=10,
            unit=UsageUnit.Seconds,
            labels={**common, "gpu_count": "0"},
            metadata=window,
            created_at=report_start + timedelta(minutes=5),
        )
    )

    report_end = report_start + timedelta(hours=2)
    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=report_start,
        end=report_end,
        bucket_seconds=3600,
    )
    overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=report_start,
        end=report_end,
        bucket_seconds=3600,
    )

    assert overview.summary == report.summary


def test_container_disk_occupancy_is_not_counted_as_task_runs(
    isolated_services: ApiServices,
) -> None:
    """A metered quantity with no billing branch contributes nothing.

    Disk byte-seconds reaching a task counter put ten figures into a workspace's
    task total, and `runs` is a 32-bit column.
    """

    report_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    labels = {"cpu_millicores": "1000", "mem_mb": "1024", "gpu_count": "0"}
    isolated_services.usage.record(
        workspace_id=workspace_id,
        resource_type="container",
        resource_id="container-disk",
        metric=UsageMetric.ContainerDurationMilliseconds,
        quantity=1_000,
        unit=UsageUnit.Milliseconds,
        labels=labels,
    )
    isolated_services.usage.record(
        workspace_id=workspace_id,
        resource_type="container",
        resource_id="container-disk",
        metric=UsageMetric.ContainerDiskByteSeconds,
        quantity=1024**3 * 30,
        unit=UsageUnit.ByteSeconds,
        labels=labels,
    )

    overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=report_start,
        end=report_start + timedelta(hours=2),
        bucket_seconds=3600,
    )

    assert sum(app.tasks for app in overview.apps) == 0


def test_billing_activity_uses_authoritative_metering_window_start(
    isolated_services: ApiServices,
) -> None:
    report_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    metering_start = report_start + timedelta(minutes=55)
    metering_end = metering_start + timedelta(minutes=10)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    isolated_services.usage.append(
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-authoritative-window",
            metric=UsageMetric.CpuSeconds,
            quantity=10,
            unit=UsageUnit.Seconds,
            metadata={
                "worker_id": "worker-authoritative-window",
                "window_start_ms": 0,
                "window_end_ms": 600_000,
                METERING_WINDOW_STARTED_AT_METADATA_KEY: metering_start.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: metering_end.isoformat(),
            },
            created_at=report_start + timedelta(hours=1, minutes=5),
        ),
    )

    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=report_start,
        end=report_start + timedelta(hours=2),
        bucket_seconds=3600,
    )

    assert report.activity[0].lines[0].quantity == 10
    assert report.activity[1].lines == ()


def test_self_hosted_container_evidence_prices_at_nothing(
    isolated_services: ApiServices,
) -> None:
    """Hardware somebody brought is evidence of compute we never sold.

    It is retained because the dashboard shows what ran, and priced at nothing
    because we neither bought the machine nor manage it. A connected cloud
    account looks identical here and must not be dropped with it: that is
    capacity we provision, and its rows are what the management fee is computed
    from.
    """

    now = utc_now()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    container_labels = {
        "billing_owner": UsageBillingOwner.SelfHosted.value,
        "cpu_millicores": "1000",
        "mem_mb": "1024",
        "gpu_count": "0",
        "worker_id": "worker-managed",
    }
    window: dict[str, JsonValue] = {
        "worker_id": "worker-managed",
        "window_start_ms": 0,
        "window_end_ms": 1000,
    }
    records = (
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-managed",
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=1_000,
            unit=UsageUnit.Milliseconds,
            labels=container_labels,
            metadata=window,
            created_at=now,
        ),
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="container",
            resource_id="container-managed",
            metric=UsageMetric.CpuSeconds,
            quantity=1,
            unit=UsageUnit.Seconds,
            labels=container_labels,
            metadata=window,
            created_at=now,
        ),
    )
    for record in records:
        isolated_services.usage.append(record)

    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        bucket_seconds=3600,
    )
    retained = isolated_services.usage.list(UsageQuery(workspace_id=workspace_id))
    overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        bucket_seconds=3600,
    )

    # Retained as evidence of what ran, priced at nothing on both paths rather
    # than twice on one.
    assert len(retained) == len(records)
    assert report.summary == ()
    assert report.total_cost_nanos == 0
    assert overview.summary == report.summary
    assert overview.total_cost_nanos == report.total_cost_nanos


def test_connected_cloud_container_evidence_survives_to_be_priced(
    isolated_services: ApiServices,
) -> None:
    """A customer's own cloud account is capacity we manage, so it still bills.

    Both classifications reach the report as private capacity, and one drop site
    covering both would silently waive every management fee on the connected
    accounts the fee exists for.
    """

    now = utc_now()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    container_labels = {
        "billing_owner": UsageBillingOwner.ConnectedCloud.value,
        "cpu_millicores": "1000",
        "mem_mb": "1024",
        "gpu_count": "0",
        "worker_id": "worker-connected",
    }
    window: dict[str, JsonValue] = {
        "worker_id": "worker-connected",
        "window_start_ms": 0,
        "window_end_ms": 1000,
    }
    # Two metrics in one window, as a worker actually emits them: the second
    # recomputes the window through the upsert's conflict branch rather than
    # inserting a second row.
    for metric, quantity, unit in (
        (UsageMetric.ContainerDurationMilliseconds, 1_000, UsageUnit.Milliseconds),
        (UsageMetric.CpuSeconds, 1, UsageUnit.Seconds),
    ):
        isolated_services.usage.append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="container",
                resource_id="container-connected",
                metric=metric,
                quantity=quantity,
                unit=unit,
                labels=container_labels,
                metadata=window,
                created_at=now,
            )
        )

    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        bucket_seconds=3600,
    )
    overview = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        bucket_seconds=3600,
    )

    with isolated_services.context.database.session() as session:
        rollup = UsageBillingRepository(session).aggregates(
            workspace_id=workspace_id,
            start=now - timedelta(hours=1),
            end=now + timedelta(hours=1),
            bucket_seconds=3600,
            group_by_workload=False,
        )

    # Both paths, because they drop independently: the overview reads the
    # rollup, and that is the one an invoice is computed from.
    assert report.summary != ()
    assert overview.summary != ()
    # The classification reaches the rollup, which is where the fee will read it
    # from. A column that stayed empty would price every managed account at zero
    # without failing anything.
    assert [row.billing_owner for row in rollup] == [UsageBillingOwner.ConnectedCloud.value]
    # A management fee, not a compute charge: the same seconds on the platform
    # fleet would price at the catalog CPU and memory rates, which are more than
    # an order of magnitude above these.
    assert {line.metric for line in report.summary} == {
        BillableMetric.ManagedCpuSeconds,
        BillableMetric.ManagedMemoryGibSeconds,
    }
    # The two paths compute the fee independently — one from evidence rows, one
    # from the rollup — and only agree if they resolve the owner the same way.
    assert overview.summary == report.summary
    assert overview.total_cost_nanos == report.total_cost_nanos


def test_billing_api_returns_compact_overview_and_lazy_workload_detail(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    now = utc_now()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    isolated_services.usage.record(
        workspace_id=workspace_id,
        resource_type="container",
        resource_id="container-estimate",
        metric=UsageMetric.ContainerDurationMilliseconds,
        quantity=1_000,
        unit=UsageUnit.Milliseconds,
        labels={"cpu_millicores": "1000", "mem_mb": "1024", "gpu_count": "0"},
    )

    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    params: dict[str, str | int] = {
        "start": (now - timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
        "bucket_seconds": 3600,
    }
    response = client.get(
        "/api/v1/usage/billing",
        params=params,
        headers=_auth_headers(isolated_services),
    )
    detail_response = client.get(
        "/api/v1/usage/billing/workloads",
        params={**params, "app_id": ""},
        headers=_auth_headers(isolated_services),
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "workspace_id",
        "start",
        "end",
        "currency",
        "total_cost_nanos",
        "summary",
        "apps",
        "activity",
    }
    assert payload["summary"][0]["metric"] == "cpu_seconds"
    assert set(payload["apps"][0]) == {
        "app_id",
        "app_name",
        "tasks",
        "total_cost_nanos",
        "lines",
    }
    assert (
        sum(bucket["total_cost_nanos"] for bucket in payload["activity"])
        == payload["total_cost_nanos"]
    )
    assert payload["currency"] == "USD"
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["app_id"] == ""
    assert detail["data"][0]["workload_name"] == "Unlinked workload"
    assert detail["data"][0]["total_cost_nanos"] == payload["total_cost_nanos"]


def test_billing_api_resolves_typed_current_period_on_the_backend(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    before = utc_now()
    response = client.get(
        "/api/v1/usage/billing",
        params={"period": UsageBillingPeriod.Current.value, "bucket_seconds": 86_400},
        headers=_auth_headers(isolated_services),
    )
    after = utc_now()

    assert response.status_code == 200
    payload = response.json()
    resolved_start = datetime.fromisoformat(payload["start"])
    resolved_end = datetime.fromisoformat(payload["end"])
    assert resolved_start == before.astimezone(UTC).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    assert before <= resolved_end <= after

    mixed = client.get(
        "/api/v1/usage/billing",
        params={
            "period": UsageBillingPeriod.Current.value,
            "start": before.isoformat(),
            "end": after.isoformat(),
        },
        headers=_auth_headers(isolated_services),
    )
    invalid = client.get(
        "/api/v1/usage/billing",
        params={"period": "previous"},
        headers=_auth_headers(isolated_services),
    )
    missing = client.get(
        "/api/v1/usage/billing",
        headers=_auth_headers(isolated_services),
    )
    assert mixed.status_code == 400
    assert invalid.status_code == 422
    assert missing.status_code == 400


def test_run_activity_is_attributed_without_becoming_a_billable_metric(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    app_id = str(uuid4())
    stub_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        AppRepository(session).upsert(
            AppRecord(id=app_id, workspace_id=workspace_id, name="activity-only")
        )
        StubRepository(session).upsert(
            StubRecord(
                id=stub_id,
                workspace_id=workspace_id,
                app_id=app_id,
                name="ping",
                kind=StubKind.Function,
            )
        )

    isolated_services.usage.record_task_count(
        workspace_id=workspace_id,
        resource_type="function",
        resource_id=stub_id,
        task_id=str(uuid4()),
        kind="function",
        app_id=app_id,
        deployment_id="deployment-v1",
    )

    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=now - timedelta(hours=1),
        end=now + timedelta(hours=1),
        bucket_seconds=3600,
    )

    assert report.total_cost_nanos == 0
    assert report.summary == ()
    assert len(report.apps) == 1
    assert report.apps[0].tasks == 1
    assert len(report.workloads) == 1
    assert report.workloads[0].tasks == 1
    assert report.coverage.status is BillingCoverageStatus.Empty


def test_usage_price_catalog_is_validated_from_deployment_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    price_base: dict[str, JsonValue] = {
        "price_per_unit_nanos": 25000,
        "currency": "EUR",
        "effective_date": "2025-01-01",
    }
    prices: list[dict[str, JsonValue]] = [
        {**price_base, "metric": "cpu_seconds", "label": "CPU", "unit": "seconds"},
        {
            **price_base,
            "metric": "memory_gib_seconds",
            "label": "Memory",
            "unit": "gib_seconds",
        },
        # Derived from the same list the catalog validates against: a fixture
        # that hand-listed models would drift from it exactly as two catalogs do.
        *(
            {
                **price_base,
                "metric": "gpu_seconds",
                "label": f"GPU ({gpu.value})",
                "unit": "seconds",
                "variant": gpu.value,
            }
            for gpu in SUPPORTED_GPU_TYPES
        ),
        {**price_base, "metric": "managed_cpu_seconds", "label": "Managed CPU", "unit": "seconds"},
        {
            **price_base,
            "metric": "managed_memory_gib_seconds",
            "label": "Managed memory",
            "unit": "gib_seconds",
        },
        *(
            {
                **price_base,
                "metric": "managed_gpu_seconds",
                "label": f"Managed GPU ({gpu.value})",
                "unit": "seconds",
                "variant": gpu.value,
            }
            for gpu in SUPPORTED_GPU_TYPES
        ),
    ]
    monkeypatch.setenv("LAZYCLOUD_USAGE_BILLING_CURRENCY", "eur")
    monkeypatch.setenv("LAZYCLOUD_USAGE_PRICE_CATALOG", json.dumps(prices))

    configured = UsagePricingSettings()

    assert configured.billing_currency == "EUR"
    assert configured.price_catalog is not None
    assert (
        configured.to_price_catalog().price_for(
            BillableMetric.GpuSeconds, "H100", on=utc_now().date()
        )
        is not None
    )

    monkeypatch.setenv("LAZYCLOUD_USAGE_PRICE_CATALOG", json.dumps([prices[0], *prices]))
    with pytest.raises(ValidationError, match="duplicate usage price for cpu_seconds"):
        UsagePricingSettings()

    monkeypatch.setenv("LAZYCLOUD_USAGE_PRICE_CATALOG", json.dumps(prices[:1]))
    with pytest.raises(ValidationError, match="must cover exactly the priced metrics"):
        UsagePricingSettings()


def test_billing_csv_matches_authorized_report_and_preserves_all_sections(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    now = utc_now()
    other_workspace = owned_workspace(ControlPlaneService(isolated_services.context), "external")
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        other_workspace_id = other_workspace.id
    isolated_services.usage.record(
        workspace_id=workspace_id,
        resource_type="container",
        resource_id="container-csv",
        metric=UsageMetric.ContainerDurationMilliseconds,
        quantity=1_000,
        unit=UsageUnit.Milliseconds,
        labels={"cpu_millicores": "1000", "mem_mb": "1024", "gpu_count": "0"},
    )
    params: dict[str, str | int] = {
        "start": (now - timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
        "bucket_seconds": 3600,
    }
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = _auth_headers(isolated_services)

    report_response = client.get("/api/v1/usage/billing", params=params, headers=headers)
    csv_response = client.get("/api/v1/usage/billing.csv", params=params, headers=headers)
    denied_response = client.get(
        "/api/v1/usage/billing.csv",
        params={**params, "workspace": "default"},
        headers=_auth_headers(isolated_services, workspace_id=other_workspace_id),
    )
    naive_response = client.get(
        "/api/v1/usage/billing",
        params={"start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00"},
        headers=headers,
    )

    assert report_response.status_code == 200
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"] == "text/csv; charset=utf-8"
    assert csv_response.headers["content-disposition"].startswith('attachment; filename="usage-')
    rows = list(DictReader(StringIO(csv_response.text)))
    assert {row["section"] for row in rows} >= {
        "report",
        "catalog",
        "summary",
        "app",
        "workload",
        "activity",
        "coverage",
    }
    report_row = next(row for row in rows if row["section"] == "report")
    assert int(report_row["total_cost_nanos"]) == report_response.json()["total_cost_nanos"]
    catalog_row = next(
        row for row in rows if row["section"] == "catalog" and row["metric"] == "cpu_seconds"
    )
    # The catalog section carries the rate charged, not a citation of anyone
    # else's published price—a sell price has no source to point at.
    assert int(catalog_row["price_per_unit_nanos"]) > 0
    assert catalog_row["effective_date"]
    gpu_rows = {
        row["variant"]
        for row in rows
        if row["section"] == "catalog" and row["metric"] == "gpu_seconds"
    }
    assert {"T4", "H100"} <= gpu_rows
    assert (
        next(row for row in rows if row["section"] == "coverage")["coverage_status"] == "complete"
    )
    assert denied_response.status_code == 403
    assert naive_response.status_code == 400


def test_billing_window_normalizes_to_utc_and_is_end_exclusive(
    isolated_services: ApiServices,
) -> None:
    now = utc_now().replace(microsecond=0)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)
    for created_at, quantity in ((start, 1), (end, 100)):
        isolated_services.usage.append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="function",
                resource_id="window-function",
                metric=UsageMetric.TaskCount,
                quantity=quantity,
                unit=UsageUnit.Count,
                created_at=created_at,
            ),
        )

    mountain = timezone(timedelta(hours=-6))
    report = isolated_services.usage.billing_report(
        workspace_id=workspace_id,
        start=start.astimezone(mountain),
        end=end.astimezone(mountain),
        bucket_seconds=3600,
    )

    assert report.start.tzinfo is UTC
    assert report.end.tzinfo is UTC
    assert report.summary == ()
    assert len(report.workloads) == 1
    assert report.workloads[0].tasks == 1
    assert report.activity[0].start == start
    assert report.activity[-1].end == end


def test_agent_node_usage_records_against_canonical_workspace_id(
    isolated_services: ApiServices,
) -> None:
    compute_states = RedisComputeStateRepository(RedisClient(FakeRedis(), key_prefix="usage-node"))
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    unit = isolated_services.compute.create_unit(UnitName("usage-managed"), workspace=workspace_id)
    machine = isolated_services.compute.create_machine(
        workspace=workspace_id,
        pool=MachinePool("usage-managed"),
    )
    token_hash = hash_compute_token("agent-token")
    joined_at = utc_now() - timedelta(seconds=30)
    owner_user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        enrollment = ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                user_id=owner_user_id,
                workspace_id=workspace_id,
                capacity_owner_id=unit.capacity_owner_id,
                pool=MachinePool("usage-managed"),
                machine_id=machine.id,
                machine_fingerprint_hash=hash_compute_token("machine-1"),
                credential_hash=token_hash,
                preflight_passed=True,
                heartbeat_confirmed=True,
                schedulable=True,
                readiness_phase=MachineReadinessPhase.Ready,
                last_join_at=joined_at,
                last_heartbeat_at=joined_at,
            )
        )
    compute_states.save_agent_token_state(
        ComputeAgentTokenState(
            token_hash=token_hash,
            workspace_id=workspace_id,
            capacity_owner_id=unit.capacity_owner_id,
            pool=MachinePool("usage-managed"),
            machine_id=machine.id,
            credential_id=enrollment.id,
            credential_generation=enrollment.credential_generation,
            last_heartbeat_at=joined_at,
            metadata={"pool_transport": "tsnet_restricted"},
        )
    )
    gateway = replace(
        isolated_services.gateway_service,
        compute_state=compute_states,
    )

    response = gateway.stream_agent_telemetry(
        AgentTelemetryRequest(
            agent_token="agent-token",
            metrics=AgentMetricSnapshot(
                cpu_utilization_pct=10,
                memory_used_mb=256,
                memory_total_mb=1024,
            ),
        )
    )

    summary = isolated_services.usage.aggregate(query=UsageQuery(workspace_id=workspace_id))
    by_metric = {row.metric: row.quantity for row in summary}
    records = [
        record
        for record in isolated_services.usage.list(query=UsageQuery(workspace_id=workspace_id))
        if record.metric is UsageMetric.NodeUsage
    ]

    assert response.ok is True
    assert by_metric[UsageMetric.NodeUsage] > 0
    assert records[0].labels["transport"] == "tsnet_restricted"
    assert records[0].metadata["transport"] == "tsnet_restricted"
    assert not isolated_services.usage.aggregate(query=UsageQuery(workspace_id="default"))


def _auth_headers(services: ApiServices, *, workspace_id: str = "default") -> dict[str, str]:
    raw_token, _record = AuthService(services.context).create_token(
        "usage-test",
        scopes=["read", "write"],
        workspace_id=workspace_id,
    )
    return {"Authorization": f"Bearer {raw_token}"}


def _admin_auth_headers(services: ApiServices) -> dict[str, str]:
    raw_token, _record = administrator_credential(services, "usage-admin")
    return {"Authorization": f"Bearer {raw_token}"}
