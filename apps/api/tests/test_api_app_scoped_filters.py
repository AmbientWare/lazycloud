from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisSettings
from fastapi.testclient import TestClient
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.deployments import DeploymentListResponse
from shared.http.stubs import StubListResponse
from shared.http.tasks import TaskTimeWindowBucketListResponse
from shared.identity import WorkspaceRecord
from shared.tasks import TaskStatus
from sqlalchemy import Engine
from sqlalchemy.engine import URL
from tests.backing_services import redis_url
from tests.database_fixtures import temporary_database
from tests.domain_fixtures import owned_workspace
from tests.real_redis import RealRedisActors
from tests.service_fixtures import administrator_credential, service_graph

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


@pytest.fixture(scope="module")
def listing_runtime(
    postgres_admin: Engine,
    migrated_template_url: URL,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[ApiServices, TestClient]]:
    actors = RealRedisActors(redis_url(), f"lazycloud:test:listings:{uuid4().hex}")
    try:
        with temporary_database(postgres_admin, template=migrated_template_url) as url:
            database = DatabaseClient.from_settings(
                DatabaseSettings(
                    url=url.render_as_string(hide_password=False),
                    direct_url=url.render_as_string(hide_password=False),
                    application_name=DatabaseApplicationName.Test,
                )
            )
            io = ApiAsyncIo.from_settings(
                database.settings, RedisSettings(url=actors.url, key_prefix=actors.prefix)
            )
            try:
                with (
                    service_graph(
                        database,
                        tmp_path_factory.mktemp("listing-api"),
                        redis_client=actors.client(),
                        binary_redis_client=actors.client(decode_responses=False),
                        async_io=io,
                    ) as services,
                    TestClient(create_app(services)) as client,
                ):
                    yield services, client
            finally:
                asyncio.run(io.close())
                database.dispose()
    finally:
        actors.cleanup()


@pytest.fixture
def listing_workspace(listing_runtime: tuple[ApiServices, TestClient]) -> WorkspaceRecord:
    services, _ = listing_runtime
    return owned_workspace(ControlPlaneService(services.context), f"listing-{uuid4().hex}")


@pytest.fixture
def client(
    listing_runtime: tuple[ApiServices, TestClient], listing_workspace: WorkspaceRecord
) -> Iterator[TestClient]:
    services, client = listing_runtime
    raw_token, _ = administrator_credential(services, "filters-admin")
    client.headers["Authorization"] = f"Bearer {raw_token}"
    client.params = {"workspace": listing_workspace.id}
    try:
        yield client
    finally:
        client.headers.pop("Authorization", None)
        client.params = {}
        client.cookies.clear()
        services.auth_token_cache.reset()


@pytest.mark.parametrize("resource", ["stubs", "deployments"])
def test_app_scoped_resource_lists_exclude_peer_apps(
    listing_runtime: tuple[ApiServices, TestClient],
    listing_workspace: WorkspaceRecord,
    client: TestClient,
    resource: str,
) -> None:
    services, _ = listing_runtime
    if resource == "stubs":
        control = ControlPlaneService(services.context)
        app = services.apps.create("scoped_list_app", workspace=listing_workspace.id)
        expected = control.create_stub("scoped-stub", app_id=app.id, workspace=listing_workspace.id)
        control.create_stub("peer-stub", workspace=listing_workspace.id)
        response_type = StubListResponse
        items_field = "stubs"
    else:
        expected = services.deployments.deploy(
            DeploymentSpec(
                name="scoped-deployment",
                kind=DeploymentKind.Endpoint,
                handler="scoped:handler",
                metadata={"app": "scoped_list_app"},
            ),
            workspace=listing_workspace.id,
        )
        services.deployments.deploy(
            DeploymentSpec(
                name="peer-deployment",
                kind=DeploymentKind.Endpoint,
                handler="peer:handler",
                metadata={"app": "peer_list_app"},
            ),
            workspace=listing_workspace.id,
        )
        assert expected.app_id is not None
        app = services.apps.get(expected.app_id)
        response_type = DeploymentListResponse
        items_field = "data"

    response = client.get(
        f"/api/v1/{resource}",
        params={"app_id": app.id},
    )

    assert response.status_code == 200, response.text
    payload = response_type.model_validate_json(response.content)
    items = getattr(payload, items_field)
    assert [item.id for item in items] == [expected.id]
    assert items[0].app_id == app.id


def test_deployed_stub_list_excludes_runtime_only_revisions(
    listing_runtime: tuple[ApiServices, TestClient],
    listing_workspace: WorkspaceRecord,
    client: TestClient,
) -> None:
    services, _ = listing_runtime
    control = ControlPlaneService(services.context)
    runtime = control.create_stub("runtime-only", workspace=listing_workspace.id)
    deployment = services.deployments.deploy(
        DeploymentSpec(
            name="published",
            kind=DeploymentKind.Function,
            handler="published:handler",
            metadata={"app": "published_app"},
        ),
        workspace=listing_workspace.id,
    )
    assert deployment.stub_id is not None

    response = client.get(
        "/api/v1/stubs",
        params={"deployed_only": True},
    )

    assert response.status_code == 200, response.text
    payload = StubListResponse.model_validate_json(response.content)
    assert runtime.id not in {stub.id for stub in payload.stubs}
    assert [stub.id for stub in payload.stubs] == [deployment.stub_id]


def test_deployment_pages_are_app_and_workload_scoped_with_opaque_cursors(
    listing_runtime: tuple[ApiServices, TestClient],
    listing_workspace: WorkspaceRecord,
    client: TestClient,
) -> None:
    services, _ = listing_runtime
    versions = [
        services.deployments.deploy(
            DeploymentSpec(
                name="paged-worker",
                kind=DeploymentKind.Function,
                handler="workers:run",
                metadata={"app": "paged_app"},
            ),
            workspace=listing_workspace.id,
        )
        for _ in range(5)
    ]
    unrelated = services.deployments.deploy(
        DeploymentSpec(
            name="paged-worker",
            kind=DeploymentKind.Function,
            handler="workers:run",
            metadata={"app": "other_paged_app"},
        ),
        workspace=listing_workspace.id,
    )
    app_id = versions[0].app_id
    assert app_id is not None
    assert unrelated.app_id != app_id

    cursor = ""
    received_ids: list[str] = []
    page_sizes: list[int] = []
    while True:
        response = client.get(
            "/api/v1/deployments",
            params={
                "app_id": app_id,
                "name": "paged-worker",
                "limit": 2,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        assert response.status_code == 200, response.text
        payload = DeploymentListResponse.model_validate_json(response.content)
        page_sizes.append(len(payload.data))
        received_ids.extend(item.id for item in payload.data)
        cursor = payload.next
        if not cursor:
            break
        assert not cursor.isdigit()

    assert page_sizes == [2, 2, 1]
    assert received_ids == [deployment.id for deployment in reversed(versions)]
    assert unrelated.id not in received_ids
    assert len(received_ids) == len(set(received_ids)) == 5

    invalid = client.get(
        "/api/v1/deployments",
        params={"app_id": app_id, "name": "paged-worker", "cursor": "not-a-cursor"},
    )
    assert invalid.status_code == 400


def test_aggregate_tasks_by_time_window_filters_by_stub_id(
    listing_runtime: tuple[ApiServices, TestClient],
    listing_workspace: WorkspaceRecord,
    client: TestClient,
) -> None:
    services, _ = listing_runtime
    control = ControlPlaneService(services.context)
    app = services.apps.create("aggregate_app", workspace=listing_workspace.id)
    first_stub = control.create_stub(
        "aggregate-first", app_id=app.id, workspace=listing_workspace.id
    )
    second_stub = control.create_stub(
        "aggregate-second", app_id=app.id, workspace=listing_workspace.id
    )

    def seed(name: str, *, app_id: str | None, stub_id: str | None) -> None:
        task = services.tasks.create(
            name,
            workspace_id=listing_workspace.id,
            app_id=app_id,
            stub_id=stub_id,
            command=[],
        )
        task.status = TaskStatus.Complete
        services.tasks.save(task)

    seed("first-run", app_id=app.id, stub_id=first_stub.id)
    seed("first-run-again", app_id=app.id, stub_id=first_stub.id)
    seed("second-run", app_id=app.id, stub_id=second_stub.id)
    seed("unscoped-run", app_id=None, stub_id=None)

    def bucket_total(params: dict[str, str | int]) -> int:
        response = client.get(
            "/api/v1/tasks/aggregate-by-time-window",
            params={"window_seconds": 3600, **params},
        )
        assert response.status_code == 200
        payload = TaskTimeWindowBucketListResponse.model_validate_json(response.content)
        return sum(item.count for item in payload.items)

    assert bucket_total({}) == 4
    assert bucket_total({"stub_id": first_stub.id}) == 2
    assert bucket_total({"stub_id": second_stub.id}) == 1
    assert bucket_total({"app_id": app.id}) == 3
    assert bucket_total({"app_id": app.id, "stub_id": first_stub.id}) == 2
