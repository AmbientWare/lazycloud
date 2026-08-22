from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.billing_ledger import ContainerBillingShapeRepository
from database.repositories.billing_rates import ComputeRateRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import ContainerRepository
from fastapi.testclient import TestClient
from identity.auth import TokenIssuer
from shared.billing_quotes import ContainerShape
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec
from shared.http.observability import (
    AccountActivityResponse,
    AccountActivitySeriesKind,
    AccountActivityUnit,
    AccountContainerCountsResponse,
    ContainerMetricsTimeseriesResponse,
)
from shared.identity import TokenKind, WorkspaceRole
from shared.timestamps import utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageBillingOwner,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from tests.service_fixtures import (
    administrator_credential,
    owned_workspace,
    workspace_owner_user_id,
)


def _seed_container(services: ApiServices) -> ContainerRecord:
    deployment = services.deployments.deploy(
        DeploymentSpec(name="metrics-demo", handler="pkg.module:handler")
    )
    stub = next(
        item
        for item in ControlPlaneService(services.context).list_stubs()
        if item.deployment_id == deployment.id
    )
    container = ContainerRecord(
        id=str(uuid4()),
        name="metrics-container",
        image="img-metrics",
        command=["python3.12", "-m", "runner.function"],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        status=ContainerStatus.Running,
    )
    with services.context.database.session() as session:
        return ContainerRepository(session).upsert(container)


def test_container_metrics_timeseries_empty_and_missing(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    container = _seed_container(isolated_services)

    raw_token, _ = administrator_credential(isolated_services, "metrics-reader")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}

    empty = client.get(
        f"/api/v1/metrics/containers/{container.id}/timeseries",
        headers=headers,
    )
    assert empty.status_code == 200
    assert ContainerMetricsTimeseriesResponse.model_validate_json(empty.content).points == ()

    missing = client.get(
        f"/api/v1/metrics/containers/{uuid4()}/timeseries",
        headers=headers,
    )
    assert missing.status_code == 404


def _seed_activity_container(
    services: ApiServices,
    *,
    workspace_id: str,
    app_id: str | None,
    status: ContainerStatus,
    name: str,
) -> None:
    with services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name=name,
                image="img-activity",
                command=["python3.12", "-m", "runner.function"],
                workspace_id=workspace_id,
                app_id=app_id,
                status=status,
            )
        )


def _account_token(services: ApiServices, user_id: str, name: str) -> str:
    issuer = TokenIssuer(services.context)
    with services.context.database.session() as session:
        raw_token, _ = issuer.issue_for_user(session, name, user_id=user_id, kind=TokenKind.User)
    issuer.committed()
    return raw_token


def test_account_metrics_stop_at_membership_and_keep_two_workspaces_apart(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """One account's readings, over exactly the workspaces it belongs to.

    The scope is the authorization boundary: the workspaces are resolved from the
    membership rows naming the caller rather than from anything the request
    supplies, so a workspace they are not in contributes to neither the live
    footprint nor the window — a total that reached past membership would
    disclose one customer's activity to another.

    Two workspaces may hold apps of the same name, and a band that merged them
    would carry two customers' work under a label naming neither, so the series
    are keyed by workspace and app together.
    """

    control = ControlPlaneService(isolated_services.context)
    held = control.get_workspace("default")
    owner_user_id = workspace_owner_user_id(isolated_services.context, held.id)
    second = owned_workspace(control, f"second-{uuid4().hex[:8]}")
    stranger = owned_workspace(control, f"stranger-{uuid4().hex[:8]}")
    with isolated_services.context.database.session() as session:
        WorkspaceMemberRepository(session).add(
            workspace_id=second.id, user_id=owner_user_id, role=WorkspaceRole.Member
        )

    alpha = isolated_services.apps.create("alpha", workspace=held.name).id
    beta = isolated_services.apps.create("beta", workspace=held.name).id
    gamma = isolated_services.apps.create("gamma", workspace=held.name).id
    # The same app name in a second workspace: one band each, never one merged.
    second_alpha = isolated_services.apps.create("alpha", workspace=second.name).id
    unreachable = isolated_services.apps.create("zulu", workspace=stranger.name).id

    seeded = (
        (held.id, alpha, ContainerStatus.Running, "alpha-0"),
        (held.id, alpha, ContainerStatus.Running, "alpha-1"),
        (held.id, alpha, ContainerStatus.Running, "alpha-2"),
        (held.id, beta, ContainerStatus.Running, "beta-0"),
        (held.id, beta, ContainerStatus.Pending, "beta-1"),
        (held.id, gamma, ContainerStatus.Running, "gamma-0"),
        # Finished, so it is a start the window counts and not capacity held.
        (held.id, None, ContainerStatus.Exited, "loose-0"),
        (second.id, second_alpha, ContainerStatus.Running, "second-alpha-0"),
        (second.id, second_alpha, ContainerStatus.Running, "second-alpha-1"),
        (stranger.id, unreachable, ContainerStatus.Running, "zulu-0"),
    )
    for workspace_id, app_id, status, name in seeded:
        _seed_activity_container(
            isolated_services,
            workspace_id=workspace_id,
            app_id=app_id,
            status=status,
            name=name,
        )

    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {
        "Authorization": f"Bearer {_account_token(isolated_services, owner_user_id, 'metrics')}"
    }

    counts = client.get("/api/v1/metrics/account/containers", headers=headers)
    assert counts.status_code == 200
    live = AccountContainerCountsResponse.model_validate_json(counts.content)
    assert (live.running, live.pending) == (7, 1), (
        "the live footprint is not the caller's workspaces: "
        f"{live.running} running against six here and two next door, with the stranger's excluded"
    )

    activity = client.get(
        "/api/v1/metrics/account/activity",
        headers=headers,
        params={"limit": 4},
    )
    assert activity.status_code == 200
    window = AccountActivityResponse.model_validate_json(activity.content)
    assert window.unit is AccountActivityUnit.Starts
    assert window.total == 9, f"a start outside membership reached the window: {window.total}"
    assert [series.kind for series in window.series] == [
        AccountActivitySeriesKind.App,
        AccountActivitySeriesKind.App,
        AccountActivitySeriesKind.App,
        AccountActivitySeriesKind.Unassigned,
        AccountActivitySeriesKind.Other,
    ]
    named = [(series.workspace_name, series.app_name) for series in window.series[:3]]
    assert named == [(held.name, "alpha"), (second.name, "alpha"), (held.name, "beta")], (
        f"two workspaces' apps of one name were not kept apart: {named}"
    )
    # Every series spans the whole window, so a quiet interval reads as a zero
    # rather than as an interval nobody measured.
    assert {len(series.buckets) for series in window.series} == {24}
    assert sum(bucket.value for series in window.series for bucket in series.buckets) == (
        window.total
    )


_HELD_WINDOW_AGO = timedelta(minutes=10)
_HELD_RATE_BEFORE = timedelta(minutes=1)
_HELD_SECONDS = 120
_HELD_MILLICORES = 2_000
_HELD_MEMORY_MIB = 4_096


def test_account_activity_reads_held_resources_from_the_priced_ledger(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """A resource band is the capacity a placement held, at the level it held it.

    The ledger is the only record of what an app held per instant, and it is the
    record the customer is invoiced from, so a chart drawn from anything else
    would be a second answer to what an app used. Two processor-seconds a second
    is two cores, whatever interval width the reader asked for — a level divided
    by seconds, not a sum that grows with the bucket.

    A card nobody asked for is a real zero and reads as a flat band: an account
    that runs no GPU has an answer to "how many GPUs", and it is not an error and
    not an empty window.
    """

    # A window that has already happened, so every interval of it is complete:
    # a level divided by the seconds an interval covers is only the level held
    # once those seconds have passed.
    started_at = utc_now() - _HELD_WINDOW_AGO
    ended_at = started_at + timedelta(seconds=_HELD_SECONDS)
    control = ControlPlaneService(isolated_services.context)
    held = control.get_workspace("default")
    owner_user_id = workspace_owner_user_id(isolated_services.context, held.id)
    app_id = isolated_services.apps.create("held_app", workspace=held.name).id

    shape = ContainerShape(
        billing_owner=UsageBillingOwner.PlatformFleet,
        gpu_type="",
        cpu_millicores=_HELD_MILLICORES,
        memory_mib=_HELD_MEMORY_MIB,
        gpu_count=0,
    )
    container_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="held-container",
                image="img-held",
                command=["python3.12", "-m", "runner.function"],
                workspace_id=held.id,
                app_id=app_id,
                status=ContainerStatus.Running,
            )
        )
        ContainerBillingShapeRepository(session).record(
            container_id=container_id,
            workspace_id=held.id,
            shape=shape,
        )
        ComputeRateRepository(session).publish(
            billing_owner=UsageBillingOwner.PlatformFleet,
            gpu_type="",
            pricing_version="test.account-activity",
            effective_at=started_at - _HELD_RATE_BEFORE,
            nanos_per_container_second=Decimal(1),
            nanos_per_cpu_core_second=Decimal(1),
            nanos_per_memory_gib_second=Decimal(1),
            nanos_per_gpu_card_second=Decimal(1),
        )

    isolated_services.usage.append(
        UsageRecord(
            id=str(uuid4()),
            workspace_id=held.id,
            resource_type="container",
            resource_id=container_id,
            metric=UsageMetric.ContainerDurationMilliseconds,
            quantity=_HELD_SECONDS * 1_000,
            unit=UsageUnit.Milliseconds,
            labels={
                "app_id": app_id,
                "cpu_millicores": str(_HELD_MILLICORES),
                "mem_mb": str(_HELD_MEMORY_MIB),
                "gpu_count": "0",
            },
            metadata={
                METERING_WINDOW_STARTED_AT_METADATA_KEY: started_at.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: ended_at.isoformat(),
            },
        )
    )

    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {
        "Authorization": f"Bearer {_account_token(isolated_services, owner_user_id, 'held')}"
    }
    # One interval exactly as wide as the metered span, opened where it opened,
    # so the level the band draws is the level the placement held for all of it.
    span: dict[str, str] = {
        "window_seconds": str(_HELD_SECONDS),
        "start": started_at.isoformat(),
        "end": started_at.isoformat(),
    }

    cores = client.get(
        "/api/v1/metrics/account/activity",
        headers=headers,
        params={"measure": "cpu", **span},
    )
    assert cores.status_code == 200
    cpu = AccountActivityResponse.model_validate_json(cores.content)
    assert cpu.unit is AccountActivityUnit.Cores
    assert [series.app_name for series in cpu.series] == ["held_app"]
    assert cpu.series[0].buckets[-1].value == pytest.approx(_HELD_MILLICORES / 1_000)
    # The window reads in the same unit its intervals do: an amount over the
    # whole span, never the intervals' own levels added up.
    assert cpu.total == pytest.approx(_HELD_MILLICORES / 1_000)

    memory = client.get(
        "/api/v1/metrics/account/activity",
        headers=headers,
        params={"measure": "memory", **span},
    )
    assert memory.status_code == 200
    gibibytes = AccountActivityResponse.model_validate_json(memory.content)
    assert gibibytes.unit is AccountActivityUnit.Gibibytes
    assert gibibytes.series[0].buckets[-1].value == pytest.approx(_HELD_MEMORY_MIB / 1_024)
    assert gibibytes.total == pytest.approx(_HELD_MEMORY_MIB / 1_024)

    cards = client.get(
        "/api/v1/metrics/account/activity",
        headers=headers,
        params={"measure": "gpu", **span},
    )
    assert cards.status_code == 200
    gpus = AccountActivityResponse.model_validate_json(cards.content)
    assert gpus.unit is AccountActivityUnit.Gpus
    assert [series.app_name for series in gpus.series] == ["held_app"], (
        "a resource held at zero came back as an absent series rather than a flat band"
    )
    assert gpus.total == 0
    assert all(bucket.value == 0 for bucket in gpus.series[0].buckets)
