from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace
from typing import Protocol
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.apps import AppService
from control.deployment_registration import DeploymentRegistrationService
from control.deployments import DeploymentAppResolution, DeploymentRegistration
from control.service import ControlPlaneService
from database.records.apps import AppRecord
from database.repositories.apps import DeploymentRepository
from database.repositories.cleanup import (
    OBJECT_CLEANUP_SOURCE,
    CleanupRepository,
)
from database.repositories.storage import ObjectRepository
from fastapi.testclient import TestClient
from operations.management import ManagementService
from pydantic import JsonValue, TypeAdapter
from shared.app_identity import FUNCTION_IMAGE
from shared.containers import ContainerStatus
from shared.deployment_records import Deployment, DeploymentSpec
from shared.deployments import DeploymentKind, StubKind
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.client_manifests import (
    ClientContract,
    ClientOperation,
    ClientOperationName,
    ClientParameter,
)
from shared.objects import ObjectRecord
from shared.timestamps import utc_now
from shared.workload_config import StubConfig
from tests.real_redis import RealRedisActors
from tests.scheduler_composition import services_with_redis_container_control
from tests.service_fixtures import administrator_credential, owned_workspace

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _HttpResponse(Protocol):
    @property
    def content(self) -> bytes: ...


class _ClaimingAppRegistry:
    def __init__(self, apps: AppService, object_id: str) -> None:
        self.apps = apps
        self.object_id = object_id

    def create(
        self,
        name: str,
        *,
        stub_id: str | None = None,
        workspace: str = "default",
        version: int = 1,
        public: bool = False,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> AppRecord:
        if stub_id is not None:
            with self.apps.context.database.session() as session:
                CleanupRepository(session).mark_object_claimed(
                    self.object_id,
                    claimed_at=utc_now(),
                    cleanup_kind=OBJECT_CLEANUP_SOURCE,
                )
        return self.apps.create(
            name,
            stub_id=stub_id,
            workspace=workspace,
            version=version,
            public=public,
            metadata=metadata,
        )

    def get(
        self,
        app_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> AppRecord:
        return self.apps.get(app_id_or_name, workspace=workspace)

    def list(
        self,
        *,
        workspace: str | None = None,
        active: bool | None = None,
    ) -> list[AppRecord]:
        return self.apps.list(workspace=workspace, active=active)


class _RecordingPlacementResources:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def reconcile_deployments(
        self,
        *,
        workspace: str,
        required: bool = True,
    ) -> None:
        self.calls.append((workspace, required))


class _FailingDeploymentRegistrar:
    def resolve_deployment_app(
        self,
        spec: DeploymentSpec,
        *,
        workspace: str = "default",
    ) -> DeploymentAppResolution:
        del spec, workspace
        return DeploymentAppResolution(app_id=None, app_name="failing")

    def register_deployment(
        self,
        deployment: Deployment,
        *,
        workspace: str = "default",
    ) -> DeploymentRegistration:
        raise RuntimeError(f"registration failed for {deployment.id} in {workspace}")


class _FailingPlacementCleanup(_RecordingPlacementResources):
    def reconcile_deployments(
        self,
        *,
        workspace: str,
        required: bool = True,
    ) -> None:
        super().reconcile_deployments(workspace=workspace, required=required)
        if not required:
            raise RuntimeError("placement cleanup failed")


def test_deployment_versions_are_scoped_by_kind_and_keep_versioned_stubs(
    isolated_services: ApiServices,
) -> None:
    first_function = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="shared",
            kind=DeploymentKind.Function,
            handler="pkg:function",
        )
    )
    endpoint = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="shared",
            kind=DeploymentKind.Endpoint,
            handler="pkg:endpoint",
        )
    )
    second_function = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="shared",
            kind=DeploymentKind.Function,
            handler="pkg:function_v2",
        )
    )

    assert first_function.version == 1
    assert endpoint.version == 1
    assert second_function.version == 2
    assert first_function.stub_id is not None
    assert endpoint.stub_id is not None
    assert second_function.stub_id is not None

    stubs_by_deployment = {
        stub.deployment_id: stub
        for stub in ControlPlaneService(isolated_services.context).list_stubs()
        if stub.deployment_id
    }
    assert stubs_by_deployment[first_function.id].kind.value == DeploymentKind.Function.value
    assert stubs_by_deployment[endpoint.id].kind.value == DeploymentKind.Endpoint.value
    assert stubs_by_deployment[second_function.id].handler == "pkg:function_v2"
    assert stubs_by_deployment[first_function.id].id == first_function.stub_id
    assert stubs_by_deployment[endpoint.id].id == endpoint.stub_id
    assert stubs_by_deployment[second_function.id].id == second_function.stub_id

    latest_resources = isolated_services.deployment_resources.list(
        kinds={DeploymentKind.Function, DeploymentKind.Endpoint},
        latest_per_resource=True,
    )
    latest_by_kind = {
        resource.deployment.kind: resource.deployment for resource in latest_resources
    }
    assert latest_by_kind[DeploymentKind.Function].id == second_function.id
    assert latest_by_kind[DeploymentKind.Endpoint].id == endpoint.id

    resource = isolated_services.deployment_resources.get_by_deployment_id(first_function.id)
    assert resource is not None
    assert resource.deployment.id == first_function.id
    assert resource.stub.id == first_function.stub_id


def test_registration_failure_tombstones_deployment_and_reconciles_placement(
    isolated_services: ApiServices,
) -> None:
    control_plane = ControlPlaneService(
        isolated_services.context,
        workspace_changes=isolated_services.workspace_changes,
    )
    workspace = owned_workspace(control_plane, "default")
    source_object = ObjectRecord(
        id=str(uuid4()),
        bucket="objects",
        key="sources/registration-failure.zip",
        path="/objects/registration-failure.zip",
        size=6,
        sha256="source",
    )
    with isolated_services.context.database.session() as session:
        ObjectRepository(session).upsert(source_object, workspace_id=workspace.id)
    source_stub = control_plane.create_stub(
        "registration-source",
        workspace=workspace.id,
        config=StubConfig(object_id=source_object.id),
    )
    placements = _RecordingPlacementResources()
    deployments = replace(
        isolated_services.deployments,
        registrar=DeploymentRegistrationService(
            _ClaimingAppRegistry(isolated_services.apps, source_object.id),
            control_plane,
        ),
        placement_resources=placements,
    )
    with pytest.raises(ConflictError, match="cleanup is in progress"):
        deployments.deploy(
            DeploymentSpec(
                name="registration-failure",
                kind=DeploymentKind.Function,
                handler="pkg:function",
                metadata={"stub_id": source_stub.id},
            ),
            workspace=workspace.id,
        )

    with isolated_services.context.database.session() as session:
        failed = [
            deployment
            for deployment in DeploymentRepository(session).list(
                workspace_id=workspace.id,
                include_deleted=True,
            )
            if deployment.name == "registration-failure"
        ]
    assert len(failed) == 1
    assert not failed[0].active
    assert failed[0].deleted_at is not None
    assert failed[0].app_id is None
    assert failed[0].stub_id is None
    assert placements.calls == [(workspace.id, True), (workspace.id, False)]
    assert isolated_services.apps.list(workspace=workspace.id) == []
    remaining_stubs = control_plane.list_stubs(workspace=workspace.id)
    assert [stub.id for stub in remaining_stubs] == [source_stub.id]
    assert all(stub.deployment_id != failed[0].id for stub in remaining_stubs)

    retry = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="registration-failure",
            kind=DeploymentKind.Function,
            handler="pkg:function",
        ),
        workspace=workspace.id,
    )
    assert retry.version == 2
    assert retry.active
    assert retry.stub_id is not None
    with pytest.raises(ConflictError, match="already bound"):
        control_plane.discard_deployment_registration_stub(
            retry.stub_id,
            deployment_id=retry.id,
            workspace=workspace.id,
        )
    assert control_plane.get_stub(retry.stub_id, workspace=workspace.id).id == retry.stub_id


def test_registration_and_placement_cleanup_failures_are_both_reported(
    isolated_services: ApiServices,
) -> None:
    workspace = owned_workspace(ControlPlaneService(isolated_services.context), "default")
    placements = _FailingPlacementCleanup()
    deployments = replace(
        isolated_services.deployments,
        registrar=_FailingDeploymentRegistrar(),
        placement_resources=placements,
    )
    with pytest.raises(ExceptionGroup) as raised:
        deployments.deploy(
            DeploymentSpec(
                name="failed-compensation",
                kind=DeploymentKind.Function,
                handler="pkg:function",
            ),
            workspace=workspace.id,
        )

    messages = [str(error) for error in raised.value.exceptions]
    assert any("registration failed" in message for message in messages)
    assert any("placement cleanup failed" in message for message in messages)
    with isolated_services.context.database.session() as session:
        failed = [
            deployment
            for deployment in DeploymentRepository(session).list(
                workspace_id=workspace.id,
                include_deleted=True,
            )
            if deployment.name == "failed-compensation"
        ]
    assert len(failed) == 1
    assert failed[0].deleted_at is not None
    assert placements.calls == [(workspace.id, True), (workspace.id, False)]


def test_new_deployment_version_keeps_prior_versions_invokable(
    isolated_services: ApiServices,
) -> None:
    resources = isolated_services.deployment_resources
    v1 = isolated_services.deployments.deploy(
        DeploymentSpec(name="predict", kind=DeploymentKind.Function, handler="pkg:v1")
    )
    v2 = isolated_services.deployments.deploy(
        DeploymentSpec(name="predict", kind=DeploymentKind.Function, handler="pkg:v2")
    )

    assert isolated_services.deployments.get(v1.id).active
    assert isolated_services.deployments.get(v2.id).active

    latest = resources.resolve_invoke_target(
        "predict", DeploymentKind.Function, workspace="default"
    )
    assert latest.deployment.id == v2.id

    versioned = resources.resolve_invoke_target(
        "predict", DeploymentKind.Function, workspace="default", version=1
    )
    assert versioned.deployment.id == v1.id


def test_invoke_target_never_falls_back_when_latest_version_is_stopped(
    isolated_services: ApiServices,
) -> None:
    management = ManagementService(isolated_services)
    resources = isolated_services.deployment_resources
    v1 = isolated_services.deployments.deploy(
        DeploymentSpec(name="predict", kind=DeploymentKind.Function, handler="pkg:v1")
    )
    v2 = isolated_services.deployments.deploy(
        DeploymentSpec(name="predict", kind=DeploymentKind.Function, handler="pkg:v2")
    )

    management.set_deployment_active("default", v2.id, active=False)

    with pytest.raises(InvalidInputError, match="not active: predict v2"):
        resources.resolve_invoke_target("predict", DeploymentKind.Function, workspace="default")
    with pytest.raises(InvalidInputError, match="not active: predict v2"):
        management.deployment_url_by_name("default", StubKind.Function, "predict")

    still_versioned = resources.resolve_invoke_target(
        "predict", DeploymentKind.Function, workspace="default", version=1
    )
    assert still_versioned.deployment.id == v1.id

    management.set_deployment_active("default", v1.id, active=False)
    with pytest.raises(InvalidInputError, match="not active: predict v1"):
        resources.resolve_invoke_target(
            "predict", DeploymentKind.Function, workspace="default", version=1
        )

    management.set_deployment_active("default", v2.id, active=True)
    restarted = resources.resolve_invoke_target(
        "predict", DeploymentKind.Function, workspace="default"
    )
    assert restarted.deployment.id == v2.id

    with pytest.raises(NotFoundError, match="deployment not found"):
        resources.resolve_invoke_target("missing", DeploymentKind.Function, workspace="default")


def test_cron_schedule_follows_deployment_lifecycle(
    isolated_services: ApiServices,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="hourly",
            kind=DeploymentKind.Function,
            handler="pkg:hourly",
            cron="0 * * * *",
        )
    )
    # Deploying is what creates the schedule: the spec declared one, so there is
    # nothing else to call.
    assert len(isolated_services.cron_jobs.list()) == 1
    management = ManagementService(isolated_services)

    management.set_deployment_active("default", deployment.id, active=False)

    stopped = isolated_services.cron_jobs.list()
    assert len(stopped) == 1
    assert not stopped[0].enabled

    management.set_deployment_active("default", deployment.id, active=True)

    restarted = isolated_services.cron_jobs.list()
    assert len(restarted) == 1
    assert restarted[0].enabled
    assert restarted[0].next_run_at is not None

    management.delete_deployment("default", deployment.id)

    assert isolated_services.cron_jobs.list() == []


def test_redeploying_without_cron_removes_the_schedule(
    isolated_services: ApiServices,
) -> None:
    """Deleting `cron=` from the source is what stops the schedule.

    The row is named for the subdomain, which every version of a resource
    shares, so a redeploy that declares a schedule replaces it. One that
    declares none has to say so: left alone the row survives its own source
    line, still names the version that wrote it, and keeps firing something the
    author has already deleted.
    """

    scheduled = DeploymentSpec(
        name="nightly",
        kind=DeploymentKind.Function,
        handler="pkg:nightly",
        cron="0 3 * * *",
    )
    isolated_services.deployments.deploy(scheduled)
    assert len(isolated_services.cron_jobs.list()) == 1

    isolated_services.deployments.deploy(
        DeploymentSpec(name="nightly", kind=DeploymentKind.Function, handler="pkg:nightly")
    )

    assert isolated_services.cron_jobs.list() == [], (
        "the schedule outlived the source line that asked for it, so the superseded "
        "version keeps firing on a schedule nothing in the author's code names"
    )


def test_redeploying_releases_the_prior_version_warm_floor(
    isolated_services: ApiServices,
) -> None:
    """A warm floor belongs to the resource, not to every version of it.

    It is held per stub and each version keeps its own, and a floor makes the
    idle window infinite — so without this every deploy pins another floor's
    worth of containers that no name resolves to and nothing retires. The prior
    version stays invocable by number; it just stops being warm.
    """

    control_plane = isolated_services.control_plane_service
    warm: dict[str, JsonValue] = {"autoscaler": {"min_containers": 2, "max_containers": 4}}
    v1 = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="predict",
            kind=DeploymentKind.Function,
            handler="pkg:v1",
            metadata=warm,
        )
    )
    assert control_plane.get_stub(v1.stub_id or "").config.autoscaler.min_containers == 2

    isolated_services.deployments.deploy(
        DeploymentSpec(
            name="predict",
            kind=DeploymentKind.Function,
            handler="pkg:v2",
            metadata=warm,
        )
    )

    superseded = control_plane.get_stub(v1.stub_id or "")
    assert superseded.config.autoscaler.min_containers == 0, (
        "the superseded version still holds its own warm floor, so every deploy leaves "
        "another two containers running that nothing routes to and nothing retires"
    )
    # The window stays infinite on purpose: a running container took its
    # keep-warm seconds from the environment it started with, so a finite one
    # written here would reach the config and not them. A zero floor with no
    # window is what puts them under the autoscaler, which stops the idle ones.
    assert superseded.config.runtime.keep_warm == -1
    assert isolated_services.deployments.get(v1.id).active


def test_cron_schedule_is_deleted_with_app(isolated_services: ApiServices) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="cleanup",
            kind=DeploymentKind.Function,
            handler="pkg:cleanup",
            cron="every 1m",
            metadata={"app": "cron_cleanup"},
        )
    )
    assert len(isolated_services.cron_jobs.list()) == 1
    assert deployment.app_id is not None

    isolated_services.apps.delete(deployment.app_id, workspace="default")

    assert isolated_services.cron_jobs.list() == []


def test_deployment_versions_are_scoped_by_app_slug(
    isolated_services: ApiServices,
) -> None:
    billing_first = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="api",
            kind=DeploymentKind.Endpoint,
            handler="billing:first",
            metadata={"app": "billing"},
        )
    )
    analytics_first = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="api",
            kind=DeploymentKind.Endpoint,
            handler="analytics:first",
            metadata={"app": "analytics"},
        )
    )
    billing_second = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="api",
            kind=DeploymentKind.Endpoint,
            handler="billing:second",
            metadata={"app": "billing"},
        )
    )

    assert billing_first.version == 1
    assert analytics_first.version == 1
    assert billing_second.version == 2
    assert billing_first.app_id == billing_second.app_id
    assert analytics_first.app_id != billing_first.app_id


def test_management_stop_and_delete_are_workspace_scoped_and_stop_containers(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    services = services_with_redis_container_control(
        isolated_services,
        real_redis_actors.client(),
    )
    management = ManagementService(services)
    deployment = services.deployments.deploy(
        DeploymentSpec(
            name="managed",
            kind=DeploymentKind.Function,
            handler="pkg:function",
        )
    )
    stub = next(
        stub
        for stub in ControlPlaneService(services.context).list_stubs()
        if stub.deployment_id == deployment.id
    )
    container = services.containers.run(
        "managed-worker",
        FUNCTION_IMAGE,
        ["python", "-m", "runner.function"],
        stub_id=stub.id,
    )

    stopped = management.set_deployment_active("default", "managed", active=False)

    assert stopped.id == deployment.id
    assert not stopped.active
    assert management.retrieve_deployment("default", "managed").id == deployment.id
    assert services.containers.get(container.id).status is ContainerStatus.Stopped

    deleted = management.delete_deployment("default", "managed")

    assert deleted.id == deployment.id
    assert deleted.deleted_at is not None


def test_deployment_manifest_route_serves_invoke_schema(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="square",
            kind=DeploymentKind.Function,
            handler="pkg:square",
            metadata={
                "app": "demo",
                "inputs": {"fields": {"value": {"type": "integer"}}},
                "outputs": {"fields": {"return": {"type": "integer"}}},
            },
            client_contract=ClientContract(
                operation=ClientOperation(
                    name=ClientOperationName.Remote,
                    parameters=[ClientParameter(name="value", json_schema={"type": "integer"})],
                    return_schema={"type": "integer"},
                )
            ),
        )
    )
    pod = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="pod-worker",
            kind=DeploymentKind.Pod,
            metadata={"app": "demo"},
            command=["python", "-m", "http.server", "8080"],
            ports={"http": 8080},
        )
    )

    raw_token, _ = administrator_credential(isolated_services, "manifest-admin")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}

    response = client.get(
        f"/api/v1/deployments/{deployment.id}/manifest",
        headers=headers,
        params={"external_url": "https://ui.example"},
    )
    assert response.status_code == 200
    manifest = _response_json(response)
    assert _json_path(manifest, "app") == "demo"
    assert _json_path(manifest, "name") == "square"
    assert _json_path(manifest, "kind") == "function"
    assert _json_path(manifest, "deployment_id") == deployment.id
    invoke_url = _json_path(manifest, "invoke_url")
    assert isinstance(invoke_url, str)
    assert invoke_url == f"https://{deployment.subdomain}.ui.example"
    assert _json_path(manifest, "inputs", "fields", "value", "type") == "integer"
    assert _json_path(manifest, "client_contract", "operation", "name") == "remote"
    assert _json_path(manifest, "client_contract", "operation", "parameters", 0, "name") == "value"
    assert _json_path(
        manifest,
        "client_contract",
        "operation",
        "return_schema",
    ) == {"type": "integer"}

    not_invokable = client.get(
        f"/api/v1/deployments/{pod.id}/manifest",
        headers=headers,
    )
    assert not_invokable.status_code == 400

    missing = client.get(
        "/api/v1/deployments/does-not-exist/manifest",
        headers=headers,
    )
    assert missing.status_code == 404


def _response_json(response: _HttpResponse) -> JsonValue:
    return _JSON_VALUE_ADAPTER.validate_json(response.content)


def _json_path(value: JsonValue, *path: str | int) -> JsonValue:
    current = value
    for segment in path:
        if isinstance(segment, str):
            assert isinstance(current, dict)
            current = current[segment]
        else:
            assert isinstance(current, list)
            current = current[segment]
    return current


def test_registration_keeps_a_source_stub_that_something_is_using(
    isolated_services: ApiServices,
) -> None:
    """A stub invoked before it was deployed is not swept up by deploying it.

    Registration copies its source stub forward and discards it, because the
    ordinary one is a staging row nothing refers to. A user who called the
    function before deploying leaves tasks against that row, and it stops being
    disposable the moment anything points at it.
    """

    control_plane = ControlPlaneService(
        isolated_services.context,
        workspace_changes=isolated_services.workspace_changes,
    )
    workspace = owned_workspace(control_plane, "default")
    source_stub = control_plane.create_stub(
        "used-before-deploy",
        workspace=workspace.id,
        kind=StubKind.Function,
        handler="pkg:function",
    )
    isolated_services.tasks.create(
        "invoked-before-deploy",
        workspace_id=workspace.id,
        stub_id=source_stub.id,
    )

    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="used-before-deploy",
            kind=DeploymentKind.Function,
            handler="pkg:function",
            metadata={"stub_id": source_stub.id},
        ),
        workspace=workspace.id,
    )

    assert deployment.stub_id is not None
    assert deployment.stub_id != source_stub.id
    assert control_plane.get_stub(source_stub.id, workspace=workspace.id).id == source_stub.id
