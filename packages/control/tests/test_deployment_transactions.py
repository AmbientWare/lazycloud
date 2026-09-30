from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from api.server.services import ApiServices
from control.stubs import StubService
from database.records.apps import StubRecord
from database.repositories.deployment_effects import DeploymentEffect
from database.tables.deployment_effects import DeploymentEffectTable
from operations.deployment_effects import DeploymentEffects
from operations.management import ManagementService
from shared.app_identity import FUNCTION_IMAGE
from shared.containers import ContainerStatus
from shared.deployment_records import Deployment, DeploymentSpec
from shared.errors import ConflictError, InvalidInputError
from shared.http.gateway import GetOrCreateStubRequest
from shared.timestamps import utc_now
from sqlalchemy import select, update
from tests.real_redis import RealRedisActors
from tests.scheduler_composition import services_with_redis_container_control


def test_failed_publication_preserves_prior_version_floor_schedule_and_app(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    spec = DeploymentSpec(
        name="atomic",
        handler="pkg:run",
        cron="every 1m",
        metadata={"autoscaler": {"min_containers": 2, "max_containers": 3}},
    )
    first = services.deployments.deploy(spec)
    assert first.stub_id and first.app_id
    app = services.apps.get(first.app_id)
    with pytest.raises(InvalidInputError):
        services.deployments.deploy(spec.model_copy(update={"metadata": {"allow_list": 42}}))
    assert services.deployments.list(app_id=first.app_id) == [first]
    assert services.apps.get(first.app_id) == app
    assert (
        services.control_plane_service.stubs.get_stub(
            first.stub_id
        ).config.autoscaler.min_containers
        == 2
    )
    assert services.cron_jobs.list()[0].deployment_id == first.id
    assert services.deployments.deploy(spec).version == 2


def test_concurrent_first_deploys_publish_unique_versions_and_one_warm_floor(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    barrier = Barrier(4)
    spec = DeploymentSpec(
        name="concurrent",
        handler="pkg:run",
        metadata={"autoscaler": {"min_containers": 1, "max_containers": 2}},
    )

    def deploy(_: int):
        barrier.wait(timeout=10)
        return services.deployments.deploy(spec)

    with ThreadPoolExecutor(max_workers=4) as executor:
        deployed = list(executor.map(deploy, range(4)))
    assert sorted(item.version for item in deployed) == [1, 2, 3, 4]
    assert len({item.app_id for item in deployed}) == 1
    resources = services.deployment_resources.list(name="concurrent")
    assert [
        (item.deployment.version, item.stub.config.autoscaler.min_containers) for item in resources
    ] == [(1, 0), (2, 0), (3, 0), (4, 1)]


def test_concurrent_publication_cannot_reuse_a_consumed_preparation(
    isolated_services: ApiServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = isolated_services
    prepared = services.gateway_deployment_service.get_or_create_stub(
        GetOrCreateStubRequest(name="prepared", handler="pkg:run", workspace="default")
    )
    barrier = Barrier(2)
    get_stub = StubService.get_stub

    def read_source(
        self: StubService, stub_id: str, *, workspace: str | None = "default"
    ) -> StubRecord:
        stub = get_stub(self, stub_id, workspace=workspace)
        barrier.wait(timeout=10)
        return stub

    monkeypatch.setattr(StubService, "get_stub", read_source)

    def publish(_: int) -> bool:
        try:
            services.deployments.deploy_prepared(prepared.stub_id)
        except ConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(publish, range(2))) == [False, True]
    assert len(services.deployments.list(workspace="default")) == 1


def test_interrupted_stop_replays_exact_targets_after_a_later_start(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
    real_redis_actors: RealRedisActors,
) -> None:
    services = services_with_redis_container_control(isolated_services, real_redis_actors.client())
    deployment = services.deployments.deploy(DeploymentSpec(name="recover", handler="pkg:run"))
    assert deployment.stub_id
    old = services.containers.run(
        "old", FUNCTION_IMAGE, ["python", "-m", "runner.function"], stub_id=deployment.stub_id
    )
    effects = services.deployments.effects

    def interrupted(self: DeploymentEffects, operations: list[DeploymentEffect]) -> None:
        raise RuntimeError("process interrupted after commit")

    # Interrupt the post-commit boundary; the durable operation remains the recovery input.
    with monkeypatch.context() as patch:
        patch.setattr(type(effects), "finish", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            services.deployments.set_deployment_active("default", deployment.id, active=False)
    assert not services.deployments.get(deployment.id).active
    services.deployments.set_deployment_active("default", deployment.id, active=True)
    replacement = services.containers.run(
        "replacement",
        FUNCTION_IMAGE,
        ["python", "-m", "runner.function"],
        stub_id=deployment.stub_id,
    )
    with services.context.database.session() as session:
        session.execute(
            update(DeploymentEffectTable).values(retry_at=utc_now() - timedelta(seconds=1))
        )
    effects.reconcile_pending()
    assert services.containers.get(old.id).status is ContainerStatus.Stopped
    assert services.containers.get(replacement.id).status is ContainerStatus.Pending
    with services.context.database.session() as session:
        assert session.scalar(select(DeploymentEffectTable.id)) is None
    services.containers.stop(replacement.id)


def test_latest_and_paged_deployments_keep_apps_with_the_same_workload_name(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    expected: list[Deployment] = []
    for app in ("first", "second"):
        for _ in range(3):
            expected.append(
                services.deployments.deploy(
                    DeploymentSpec(name="shared", handler="pkg:run", metadata={"app": app})
                )
            )
    management = ManagementService(services)
    assert {item.id for item in management.latest_deployments("default").data} == {
        expected[2].id,
        expected[5].id,
    }
    seen: list[str] = []
    cursor = None
    while True:
        page = management.list_deployments("default", limit=2, cursor=cursor)
        seen.extend(item.id for item in page.data)
        if not page.next:
            break
        cursor = page.next
    assert len(seen) == len(set(seen)) == len(expected)
    assert set(seen) == {item.id for item in expected}
