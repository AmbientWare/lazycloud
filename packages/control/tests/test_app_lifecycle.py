from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from database.repositories.apps import (
    AppContainerShutdownIntentRepository,
    DeploymentRepository,
)
from database.repositories.orchestration import ContainerRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from operations.app_lifecycle import ProductionAppExecutionLifecycleEffects
from operations.container_shutdown import ContainerShutdownService
from operations.management import ManagementService
from pydantic import JsonValue, TypeAdapter
from scheduler.service import Scheduler
from shared.app_identity import FUNCTION_IMAGE
from shared.app_lifecycle import AppLifecycleState
from shared.container_requests import ContainerShutdownTarget
from shared.containers import ContainerStatus
from shared.deployment_records import Deployment, DeploymentSpec
from shared.deployments import DeploymentKind
from shared.errors import ConflictError, NotFoundError, UpstreamUnavailableError
from shared.http.workspace_changes import (
    WorkspaceChangeEvent,
    WorkspaceChangeTopic,
    WorkspaceChangeType,
)
from shared.identity import AuthScope, TokenKind
from shared.scheduling import SchedulerContainerState, SchedulerContainerStatus
from shared.timestamps import utc_now
from tests.real_redis import RealRedisActors
from tests.scheduler_composition import services_with_redis_container_control

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _HttpResponse(Protocol):
    @property
    def content(self) -> bytes: ...


@dataclass(slots=True)
class _FaultingWorkspaceChanges:
    fail_before_app_once: bool = False
    fail_after_app_once: bool = False
    fail_before_deployment_number: int | None = None
    events: list[WorkspaceChangeEvent] = field(default_factory=list)
    deployment_attempts: int = 0

    def emit_change(
        self,
        *,
        workspace_id: str,
        topic: WorkspaceChangeTopic,
        change: WorkspaceChangeType,
        resource_id: str,
        app_id: str | None = None,
        deployment_id: str | None = None,
        stub_id: str | None = None,
        task_id: str | None = None,
        root_task_id: str | None = None,
        container_id: str | None = None,
        occurred_at: datetime | None = None,
        event_id: str | None = None,
    ) -> WorkspaceChangeEvent | None:
        if topic is WorkspaceChangeTopic.Deployments:
            self.deployment_attempts += 1
            if self.fail_before_deployment_number == self.deployment_attempts:
                self.fail_before_deployment_number = None
                raise RuntimeError("simulated deployment publication crash")
        if topic is WorkspaceChangeTopic.Apps and self.fail_before_app_once:
            self.fail_before_app_once = False
            return None
        event = WorkspaceChangeEvent(
            event_id=event_id or f"test-event-{len(self.events)}",
            occurred_at=occurred_at or datetime.now().astimezone(),
            workspace_id=workspace_id,
            topic=topic,
            change=change,
            resource_id=resource_id,
            app_id=app_id,
            deployment_id=deployment_id,
            stub_id=stub_id,
            task_id=task_id,
            root_task_id=root_task_id,
            container_id=container_id,
        )
        self.events.append(event)
        if topic is WorkspaceChangeTopic.Apps and self.fail_after_app_once:
            self.fail_after_app_once = False
            raise RuntimeError("simulated crash after app publication")
        return event


def test_app_pause_resume_preserves_explicitly_stopped_deployments_and_delete_cleans_up(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    services = services_with_redis_container_control(
        isolated_services,
        real_redis_actors.client(),
    )
    app = services.apps.create("lifecycle_app")
    first = services.deployments.deploy(
        DeploymentSpec(
            name="worker",
            handler="pkg:first",
            metadata={"app": app.name, "app_id": app.id},
        )
    )
    second = services.deployments.deploy(
        DeploymentSpec(
            name="worker",
            handler="pkg:second",
            metadata={"app": app.name, "app_id": app.id},
        )
    )
    management = ManagementService(services)
    management.set_deployment_active(app.workspace_id, first.id, active=False)
    container = services.containers.run(
        "live-app-container",
        FUNCTION_IMAGE,
        ["python", "-m", "runner.function"],
        workspace_id=app.workspace_id,
        stub_id=second.stub_id,
        app_id=app.id,
    )

    paused = services.apps.pause(app.id, workspace=app.workspace_id)

    assert not paused.active
    assert paused.deleted_at is None
    assert not services.deployments.get(first.id).active
    assert not services.deployments.get(second.id).active
    assert services.containers.get(container.id).status is ContainerStatus.Stopped

    resumed = services.apps.resume(app.id, workspace=app.workspace_id)

    assert resumed.active
    assert not services.deployments.get(first.id).active
    assert services.deployments.get(second.id).active

    services.apps.pause(app.id, workspace=app.workspace_id)
    deployment_while_paused = services.deployments.deploy(
        DeploymentSpec(
            name="paused_worker",
            handler="pkg:paused",
            metadata={"app": app.name, "app_id": app.id},
        )
    )
    assert not deployment_while_paused.active
    with pytest.raises(ConflictError, match="app is paused"):
        management.set_deployment_active(
            app.workspace_id,
            deployment_while_paused.id,
            active=True,
        )

    services.apps.resume(app.id, workspace=app.workspace_id)
    deletion_container = services.containers.run(
        "delete-app-container",
        FUNCTION_IMAGE,
        ["python", "-m", "runner.function"],
        workspace_id=app.workspace_id,
        stub_id=second.stub_id,
        app_id=app.id,
    )

    deleted = services.apps.delete(app.id, workspace=app.workspace_id)

    assert not deleted.active
    assert deleted.deleted_at is not None
    assert services.containers.get(deletion_container.id).status is ContainerStatus.Stopped
    assert services.apps.list(workspace=app.workspace_id) == []
    with pytest.raises(NotFoundError, match="app not found"):
        services.apps.get(app.id, workspace=app.workspace_id)
    with services.context.database.session() as session:
        repository = DeploymentRepository(session)
        for deployment_id in (first.id, second.id, deployment_while_paused.id):
            deployment = repository.get(
                deployment_id,
                workspace_id=app.workspace_id,
                include_deleted=True,
            )
            assert deployment is not None
            assert deployment.deleted_at is not None


@pytest.mark.parametrize("failure_point", ["before-stream", "after-stream"])
def test_app_terminal_publication_replays_one_durable_event_with_deterministic_id(
    isolated_services: ApiServices,
    failure_point: str,
) -> None:
    app = isolated_services.apps.create(f"terminal_replay_{failure_point.replace('-', '_')}")
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="worker",
            handler="pkg:worker",
            metadata={"app": app.name, "app_id": app.id},
        )
    )
    publisher = _FaultingWorkspaceChanges(
        fail_before_app_once=failure_point == "before-stream",
        fail_after_app_once=failure_point == "after-stream",
    )
    lifecycle = replace(
        isolated_services.apps,
        deployment_lifecycle=replace(
            isolated_services.apps.deployment_lifecycle,
            workspace_changes=publisher,
        ),
        workspace_changes=publisher,
    )

    expected_error = UpstreamUnavailableError if failure_point == "before-stream" else RuntimeError
    with pytest.raises(expected_error):
        lifecycle.pause(app.id, workspace=app.workspace_id)

    failed = isolated_services.apps.get(app.id, workspace=app.workspace_id)
    assert failed.lifecycle_state is AppLifecycleState.CleanupFailed
    app_events = isolated_services.events.list(
        workspace_id=app.workspace_id,
        resource_type="app",
        resource_id=app.id,
        actions=["app.paused"],
    )
    deployment_events = isolated_services.events.list(
        workspace_id=app.workspace_id,
        resource_type="deployment",
        resource_id=deployment.id,
        actions=["deployment.stopped"],
    )
    assert len(app_events) == 1
    assert len(deployment_events) == 1

    completed = lifecycle.pause(app.id, workspace=app.workspace_id)

    assert completed.lifecycle_state is AppLifecycleState.Paused
    replayed_app_changes = [
        event for event in publisher.events if event.topic is WorkspaceChangeTopic.Apps
    ]
    assert {event.event_id for event in replayed_app_changes} == {app_events[0].id}
    assert len(replayed_app_changes) == (1 if failure_point == "before-stream" else 2)
    assert (
        isolated_services.events.count(
            workspace_id=app.workspace_id,
            resource_type="app",
            resource_id=app.id,
            actions=["app.paused"],
        )
        == 1
    )
    assert (
        isolated_services.events.count(
            workspace_id=app.workspace_id,
            resource_type="deployment",
            resource_id=deployment.id,
            actions=["deployment.stopped"],
        )
        == 1
    )


def test_app_multi_deployment_publication_resumes_after_mid_batch_crash(
    isolated_services: ApiServices,
) -> None:
    app = isolated_services.apps.create("multi_deployment_replay")
    deployments = [
        isolated_services.deployments.deploy(
            DeploymentSpec(
                name=f"worker-{index}",
                handler=f"pkg:worker_{index}",
                metadata={"app": app.name, "app_id": app.id},
            )
        )
        for index in range(2)
    ]
    publisher = _FaultingWorkspaceChanges(fail_before_deployment_number=2)
    lifecycle = replace(
        isolated_services.apps,
        deployment_lifecycle=replace(
            isolated_services.apps.deployment_lifecycle,
            workspace_changes=publisher,
        ),
        workspace_changes=publisher,
    )

    with pytest.raises(RuntimeError, match="deployment publication crash"):
        lifecycle.pause(app.id, workspace=app.workspace_id)

    completed = lifecycle.pause(app.id, workspace=app.workspace_id)

    assert completed.lifecycle_state is AppLifecycleState.Paused
    published_deployments = [
        event.deployment_id
        for event in publisher.events
        if event.topic is WorkspaceChangeTopic.Deployments and event.deployment_id is not None
    ]
    assert sorted(published_deployments) == sorted(item.id for item in deployments)
    for deployment in deployments:
        events = isolated_services.events.list(
            workspace_id=app.workspace_id,
            resource_type="deployment",
            resource_id=deployment.id,
            actions=["deployment.stopped"],
        )
        assert len(events) == 1
        published = next(
            event for event in publisher.events if event.deployment_id == deployment.id
        )
        assert published.event_id == events[0].id


def test_scheduler_reconciles_unfinished_app_from_durable_state_after_restart(
    isolated_services: ApiServices,
) -> None:
    app = isolated_services.apps.create("scheduler_restart_replay")
    isolated_services.deployments.deploy(
        DeploymentSpec(
            name="worker",
            handler="pkg:worker",
            metadata={"app": app.name, "app_id": app.id},
        )
    )
    publisher = _FaultingWorkspaceChanges(fail_before_app_once=True)
    crashing = replace(
        isolated_services.apps,
        deployment_lifecycle=replace(
            isolated_services.apps.deployment_lifecycle,
            workspace_changes=publisher,
        ),
        workspace_changes=publisher,
    )
    with pytest.raises(UpstreamUnavailableError):
        crashing.pause(app.id, workspace=app.workspace_id)

    restarted_app_service = replace(
        isolated_services.apps,
        deployment_lifecycle=replace(
            isolated_services.apps.deployment_lifecycle,
            workspace_changes=isolated_services.workspace_changes,
        ),
        workspace_changes=isolated_services.workspace_changes,
    )
    restarted_services = replace(isolated_services, apps=restarted_app_service)

    reconciled = Scheduler(services=restarted_services).reconcile_app_lifecycle()

    assert [record.id for record in reconciled] == [app.id]
    assert reconciled[0].lifecycle_state is AppLifecycleState.Paused
    assert restarted_app_service.get(app.id, workspace=app.workspace_id).active is False


def test_app_execution_redis_cleanup_is_exact_and_preserves_peer_apps(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    target = isolated_services.apps.create("redis_cleanup_target")
    peer = isolated_services.apps.create("redis_cleanup_peer")
    target_deployments = _deploy_execution_kinds(isolated_services, target.id, target.name)
    peer_deployments = _deploy_execution_kinds(isolated_services, peer.id, peer.name)
    target_keys = _execution_redis_keys(
        redis,
        workspace_id=target.workspace_id,
        deployments=target_deployments,
    )
    peer_keys = _execution_redis_keys(
        redis,
        workspace_id=peer.workspace_id,
        deployments=peer_deployments,
    )
    for key in target_keys | peer_keys:
        assert redis.set(key, "owned")
    effects = ProductionAppExecutionLifecycleEffects(
        isolated_services.context,
        isolated_services.containers,
        isolated_services.tasks,
        redis,
        isolated_services.container_shutdowns,
    )

    effects.delete_app_execution(workspace_id=target.workspace_id, app_id=target.id)

    assert all(not redis.exists(key) for key in target_keys)
    assert all(redis.exists(key) for key in peer_keys)


def test_disconnected_worker_shutdown_remains_durable_and_retry_cleans_ack_state(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    services = services_with_redis_container_control(isolated_services, redis)
    app = services.apps.create("disconnected_worker_shutdown")
    deployment = services.deployments.deploy(
        DeploymentSpec(
            name="worker",
            handler="pkg:worker",
            metadata={"app": app.name, "app_id": app.id},
        )
    )
    assert deployment.stub_id is not None
    container = services.containers.run(
        "disconnected-app-container",
        FUNCTION_IMAGE,
        ["python", "-m", "runner.function"],
        workspace_id=app.workspace_id,
        stub_id=deployment.stub_id,
        app_id=app.id,
    )
    worker_id = "disconnected-worker"
    with services.context.database.session() as session:
        assigned = ContainerRepository(session).get_across_workspaces(container.id)
        assert assigned is not None
        ContainerRepository(session).upsert(
            assigned.model_copy(update={"runtime_worker_id": worker_id})
        )
    services.scheduler_containers.set_container_state(
        SchedulerContainerState(
            container_id=container.id,
            stub_id=deployment.stub_id,
            workspace_id=app.workspace_id,
            worker_id=worker_id,
            status=SchedulerContainerStatus.Running,
        )
    )
    fast_shutdown = ContainerShutdownService(
        services.scheduler_containers,
        services.container_shutdowns.events,
        redis,
        storage_release=services.container_shutdowns.storage_release,
        poll_interval_seconds=0.001,
    )
    crashing = replace(
        services.apps,
        execution_effects=ProductionAppExecutionLifecycleEffects(
            services.context,
            services.containers,
            services.tasks,
            redis,
            fast_shutdown,
            shutdown_timeout_seconds=0.1,
        ),
    )

    with pytest.raises(UpstreamUnavailableError, match="shutdown was not confirmed"):
        crashing.pause(app.id, workspace=app.workspace_id)

    failed = crashing.get(app.id, workspace=app.workspace_id)
    assert failed.lifecycle_state is AppLifecycleState.CleanupFailed
    with services.context.database.session() as session:
        intents = AppContainerShutdownIntentRepository(session).list(app_id=app.id)
    assert [intent.container_id for intent in intents] == [container.id]
    assert [intent.worker_id for intent in intents] == [worker_id]
    target = ContainerShutdownTarget(container_id=container.id, worker_id=worker_id)
    pending_key = redis.key("worker-events", "pending", worker_id)
    pending = {str(value) for value in redis.set_members(pending_key)}
    assert len(pending) == 1
    event_id = next(iter(pending))
    ack_key = redis.key("worker-events", "ack", event_id)
    redis.set_add(ack_key, worker_id)

    with pytest.raises(UpstreamUnavailableError, match="containers="):
        fast_shutdown.confirm([target], timeout_seconds=0.1)

    redis.delete(ack_key)
    assert services.scheduler_containers.delete_container_state(container.id)
    restarted_shutdown = ContainerShutdownService(
        services.scheduler_containers,
        services.container_shutdowns.events,
        redis,
        storage_release=services.container_shutdowns.storage_release,
        poll_interval_seconds=0.001,
    )
    with pytest.raises(UpstreamUnavailableError, match="containers="):
        restarted_shutdown.confirm([target], timeout_seconds=0.1)
    assert redis.set_members(pending_key) == {event_id}
    redis.set_add(ack_key, worker_id)
    with services.context.database.session() as session:
        ContainerRepository(session).mark_storage_released(
            container.id, worker_id=worker_id, now=utc_now()
        )
    restarted_apps = replace(
        services.apps,
        execution_effects=ProductionAppExecutionLifecycleEffects(
            services.context,
            services.containers,
            services.tasks,
            redis,
            restarted_shutdown,
            shutdown_timeout_seconds=0.1,
        ),
    )
    restarted_services = replace(services, apps=restarted_apps)

    reconciled = Scheduler(services=restarted_services).reconcile_app_lifecycle()

    assert [record.lifecycle_state for record in reconciled] == [AppLifecycleState.Paused]
    with services.context.database.session() as session:
        assert AppContainerShutdownIntentRepository(session).list(app_id=app.id) == []
    assert redis.set_members(pending_key) == set()
    assert not redis.exists(ack_key)
    assert not services.container_shutdowns.events.claim(event_id).claimed


def test_pause_fences_a_queued_container_no_worker_owns(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    """A container still waiting in the queue is fenced, not waited on.

    No worker owns it, so nothing would ever acknowledge a stop event and pause
    would block until it timed out. Leaving the request queued is worse: a
    worker could claim it afterwards and start a container for a paused app.
    """
    redis = real_redis_actors.client()
    services = services_with_redis_container_control(isolated_services, redis)
    app = services.apps.create("unowned_container_shutdown")
    deployment = services.deployments.deploy(
        DeploymentSpec(
            name="worker",
            handler="pkg:worker",
            metadata={"app": app.name, "app_id": app.id},
        )
    )
    assert deployment.stub_id is not None
    container = services.containers.run(
        "unowned-app-container",
        FUNCTION_IMAGE,
        ["python", "-m", "runner.function"],
        workspace_id=app.workspace_id,
        stub_id=deployment.stub_id,
        app_id=app.id,
    )
    assert services.scheduler_workers.has_recoverable_container_request(container.id)
    fast_shutdown = ContainerShutdownService(
        services.scheduler_containers,
        services.container_shutdowns.events,
        redis,
        storage_release=services.container_shutdowns.storage_release,
        poll_interval_seconds=0.001,
    )
    lifecycle = replace(
        services.apps,
        execution_effects=ProductionAppExecutionLifecycleEffects(
            services.context,
            services.containers,
            services.tasks,
            redis,
            fast_shutdown,
            shutdown_timeout_seconds=0.1,
        ),
    )

    paused = lifecycle.pause(app.id, workspace=app.workspace_id)

    assert paused.lifecycle_state is AppLifecycleState.Paused
    assert services.containers.get(container.id).status is ContainerStatus.Stopped
    assert services.scheduler_containers.is_container_cancelled(container.id)
    assert not services.scheduler_workers.has_recoverable_container_request(container.id)
    assert services.scheduler_workers.claim_ready_container_requests(limit=5) == []
    with services.context.database.session() as session:
        assert AppContainerShutdownIntentRepository(session).list(app_id=app.id) == []
    assert redis.scan(f"{redis.key('worker-events', 'pending')}:*") == []


def test_app_and_deployment_http_actions_follow_authorization_and_lifecycle_state(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    app = isolated_services.apps.create("dashboard_app")
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="api",
            handler="pkg:api",
            metadata={"app": app.name, "app_id": app.id},
        )
    )
    workspace = ControlPlaneService(isolated_services.context).get_workspace(app.workspace_id)
    auth = AuthService(isolated_services.context)
    writer_token, _ = auth.create_token(
        "lifecycle-writer",
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
        kind=TokenKind.Workspace,
        workspace_id=workspace.id,
    )
    reader_token, _ = auth.create_token(
        "lifecycle-reader",
        scopes=[AuthScope.Read.value],
        kind=TokenKind.Workspace,
        workspace_id=workspace.id,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    writer_headers = {"Authorization": f"Bearer {writer_token}"}
    reader_headers = {"Authorization": f"Bearer {reader_token}"}

    writer_apps = client.get("/api/v1/apps/summaries", headers=writer_headers)
    assert writer_apps.status_code == 200, writer_apps.text
    writer_items = _json_path(_response_json(writer_apps), "items")
    assert isinstance(writer_items, list)
    writer_summary = next(
        item
        for item in writer_items
        if isinstance(item, dict) and _json_path(item, "app", "id") == app.id
    )
    assert _json_path(writer_summary, "app", "actions") == {
        "can_pause": True,
        "can_resume": False,
        "can_delete": True,
    }
    assert _json_path(writer_summary, "latest_deployment", "actions") == {
        "can_start": False,
        "can_stop": True,
        "can_scale": False,
        "can_delete": True,
    }

    reader_app = client.get(f"/api/v1/apps/{app.id}", headers=reader_headers)
    assert reader_app.status_code == 200, reader_app.text
    assert _json_path(_response_json(reader_app), "actions") == {
        "can_pause": False,
        "can_resume": False,
        "can_delete": False,
    }
    reader_deployment = client.get(
        f"/api/v1/deployments/{deployment.id}",
        headers=reader_headers,
    )
    assert reader_deployment.status_code == 200, reader_deployment.text
    assert _json_path(_response_json(reader_deployment), "actions") == {
        "can_start": False,
        "can_stop": False,
        "can_scale": False,
        "can_delete": False,
    }

    pause = client.post(f"/api/v1/apps/{app.id}/pause", headers=writer_headers)
    assert pause.status_code == 200, pause.text
    pause_payload = _response_json(pause)
    assert _json_path(pause_payload, "active") is False
    assert _json_path(pause_payload, "deleted_at") is None
    assert _json_path(pause_payload, "actions") == {
        "can_pause": False,
        "can_resume": True,
        "can_delete": True,
    }
    paused_apps = client.get("/api/v1/apps", headers=writer_headers)
    paused_items = _json_path(_response_json(paused_apps), "data")
    assert isinstance(paused_items, list)
    assert app.id in [_json_path(item, "id") for item in paused_items if isinstance(item, dict)]

    paused_deployment = client.get(
        f"/api/v1/deployments/{deployment.id}",
        headers=writer_headers,
    )
    assert paused_deployment.status_code == 200, paused_deployment.text
    assert _json_path(_response_json(paused_deployment), "actions") == {
        "can_start": False,
        "can_stop": False,
        "can_scale": False,
        "can_delete": True,
    }
    blocked_start = client.post(
        f"/api/v1/deployments/{deployment.id}/start",
        headers=writer_headers,
    )
    assert blocked_start.status_code == 409, blocked_start.text

    resume = client.post(f"/api/v1/apps/{app.id}/resume", headers=writer_headers)
    assert resume.status_code == 200, resume.text
    assert _json_path(_response_json(resume), "active") is True
    assert isolated_services.deployments.get(deployment.id).active

    deleted = client.delete(f"/api/v1/apps/{app.id}", headers=writer_headers)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(f"/api/v1/apps/{app.id}", headers=writer_headers).status_code == 404
    deployments = client.get("/api/v1/deployments", headers=writer_headers)
    deployment_items = _json_path(_response_json(deployments), "data")
    assert isinstance(deployment_items, list)
    assert deployment.id not in [
        _json_path(item, "id") for item in deployment_items if isinstance(item, dict)
    ]


def _deploy_execution_kinds(
    services: ApiServices,
    app_id: str,
    app_name: str,
) -> list[Deployment]:
    deployments = [
        services.deployments.deploy(
            DeploymentSpec(
                name=f"{kind.value}-workload",
                kind=kind,
                handler=f"pkg:{kind.value}",
                metadata={"app": app_name, "app_id": app_id},
            )
        )
        for kind in (
            DeploymentKind.Endpoint,
            DeploymentKind.Pod,
        )
    ]
    assert all(deployment.stub_id is not None for deployment in deployments)
    return deployments


def _execution_redis_keys(
    redis: RedisClient,
    *,
    workspace_id: str,
    deployments: list[Deployment],
) -> set[str]:
    keys: set[str] = set()
    for deployment in deployments:
        assert deployment.stub_id is not None
        stub_id = deployment.stub_id
        if deployment.kind is DeploymentKind.Endpoint:
            root = redis.key("endpoint", workspace_id, stub_id)
            keys.update(
                {
                    root,
                    f"{root}:keep_warm_lock:container",
                    redis.key("scheduler", "serve", "lock", workspace_id, stub_id),
                    redis.key(
                        "autoscaling",
                        "endpoints",
                        workspace_id,
                        stub_id,
                        "lock",
                    ),
                }
            )
        elif deployment.kind is DeploymentKind.Pod:
            root = redis.key("pod", workspace_id, stub_id)
            keys.update(
                {
                    root,
                    f"{root}:container_connections:container",
                    redis.key(
                        "autoscaling",
                        "pods",
                        workspace_id,
                        stub_id,
                        "lock",
                    ),
                }
            )
    return keys


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
