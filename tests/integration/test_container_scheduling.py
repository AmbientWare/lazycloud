from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from coordination.redis_client import RedisClient
from database.repositories.orchestration import ContainerRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.functions.service import FunctionControlService
from execution.taskqueues.service import TaskQueueControlService
from fastapi.testclient import TestClient
from identity.auth import AuthService
from runner.invocation import cloudpickle_bytes
from scheduler.containers import SchedulerContainerSubmitResult, SchedulerContainerSubmitStatus
from scheduler.fleet import SchedulerContainerStatus
from scheduler.state import (
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
    SchedulerContainerState,
    SchedulerWorkerRequest,
)
from shared.container_requests import (
    WORKER_USER_OUTPUT_VOLUME,
    WorkerContainerRequestPayload,
)
from shared.containers import ContainerStatus
from shared.env import (
    CHECKPOINT_ENABLED_ENV,
)
from shared.errors import ConflictError
from shared.function_payloads import (
    FunctionCloudpickleInvocation,
    FunctionCloudpickleResult,
)
from shared.http.functions import (
    FUNCTION_CALL_REF_MARKER,
    FunctionCallDependency,
    FunctionGetArgsRequest,
    FunctionInvokeBody,
    FunctionSetResultBody,
)
from shared.http.taskqueues import StartTaskQueueServeRequest
from shared.identity import WorkspaceStorageConfig
from shared.tasks import TaskStatus
from shared.usage import UsageMetric
from shared.usage_query import UsageQuery
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis
from tests.scheduler_composition import scheduler_request_service_for_redis


def test_container_scheduling_failure_syncs_container_and_task(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services.containers.scheduler = scheduler

    container = isolated_services.containers.run(
        "job",
        "image-ref",
        ["python", "-c", "print(1)"],
    )
    request = scheduler.requests[0]

    ContainerSchedulingPersistenceService(
        isolated_services.context,
        isolated_services.events,
        isolated_services.workspace_changes,
    ).mark_scheduling_failed(
        request,
        "retry-limit",
        now=datetime(2026, 1, 1),
    )

    failed_container = isolated_services.containers.get(container.id)
    assert failed_container.status is ContainerStatus.Failed
    assert failed_container.exit_code == 1
    assert failed_container.finished_at is not None
    assert failed_container.task_id is not None
    task = isolated_services.tasks.get(failed_container.task_id)
    assert task.status is TaskStatus.Failed
    assert task.error == "retry-limit"
    assert task.exit_code == 1
    assert task.kwargs["container_id"] == container.id


def test_function_invoke_requests_workspace_storage_when_workspace_bucket_available(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services.containers.scheduler = scheduler
    control = ControlPlaneService(isolated_services.context)
    workspace = control.get_workspace("default")
    control.set_workspace_storage(
        workspace.id,
        WorkspaceStorageConfig(
            backend="s3",
            bucket="workspace-bucket",
            config={
                "endpoint_url": "http://object-store:9000",
                "region": "us-east-1",
                "access_key": "access",
                "secret_key": "secret",
                "force_path_style": True,
            },
        ),
    )
    stub = control.create_stub(
        "fn-storage",
        kind=StubKind.Function,
        handler="pkg.fn:handler",
        config={"runtime": {"image_id": "image-fn"}},
    )

    response = FunctionControlService(isolated_services).function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=FunctionCloudpickleInvocation.from_bytes(b"{}"),
        )
    )

    assert response.exit_code == 0
    payload = WorkerContainerRequestPayload.model_validate(scheduler.requests[0].payload)
    assert payload.workspace_storage_required
    assert [mount.mount_path for mount in payload.mounts] == [WORKER_USER_OUTPUT_VOLUME]


def test_function_dependency_waits_then_schedules_materialized_args(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services.containers.scheduler = scheduler
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub(
        "nested-fn",
        kind=StubKind.Function,
        handler="pkg.fn:handler",
        config={"runtime": {"image_id": "image-fn"}},
    )
    service = FunctionControlService(isolated_services)

    upstream = service.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=FunctionCloudpickleInvocation.from_bytes(
                cloudpickle_bytes({"args": (2,), "kwargs": {}})
            ),
        )
    )
    downstream = service.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=FunctionCloudpickleInvocation.from_bytes(
                cloudpickle_bytes(
                    {
                        "args": ({FUNCTION_CALL_REF_MARKER: True, "task_id": upstream.task_id},),
                        "kwargs": {"right": 3},
                    }
                )
            ),
            dependencies=[FunctionCallDependency(task_id=upstream.task_id)],
        )
    )

    assert upstream.exit_code == 0
    assert downstream.exit_code == 0
    assert len(scheduler.requests) == 1
    downstream_task = isolated_services.tasks.get(downstream.task_id)
    assert downstream_task.container_id is None
    assert downstream_task.parent_task_id == upstream.task_id
    assert downstream_task.root_task_id == upstream.task_id

    cloudpickle_result = FunctionCloudpickleResult.from_bytes(cloudpickle_bytes(5))
    service.function_set_result(
        FunctionSetResultBody(
            task_id=upstream.task_id,
            container_id=scheduler.requests[0].container_id,
            result=cloudpickle_result,
        )
    )

    assert len(scheduler.requests) == 2
    persisted_upstream = isolated_services.tasks.get(upstream.task_id)
    assert persisted_upstream.function_result == cloudpickle_result
    assert isinstance(persisted_upstream.function_result, FunctionCloudpickleResult)
    downstream_task = isolated_services.tasks.get(downstream.task_id)
    assert downstream_task.container_id == scheduler.requests[1].container_id
    assert persisted_upstream.function_result.bytes_value() == cloudpickle_bytes(5)
    assert len(downstream_task.dependency_bindings) == 1
    binding = downstream_task.dependency_bindings[0]
    assert binding.task_id == upstream.task_id
    assert isinstance(binding.result, FunctionCloudpickleResult)
    assert binding.result.bytes_value() == cloudpickle_bytes(5)
    graph = service.function_call_graph(downstream.task_id, workspace_id=stub.workspace_id)
    assert graph.root_task_id
    assert graph.root_task_id == upstream.task_id
    assert graph.root is not None
    assert graph.root.task_id == upstream.task_id
    assert [node.task_id for node in graph.nodes] == [upstream.task_id]
    assert [node.task_id for node in graph.nodes[0].children] == [downstream.task_id]
    assert graph.nodes[0].children[0].dependencies == [upstream.task_id]
    assert graph.nodes[0].created_at is not None
    assert graph.nodes[0].children[0].created_at is not None
    raw_token, _record = AuthService(isolated_services.context).create_token(
        "graph-test",
        scopes=["read", "write"],
        workspace_id="default",
    )
    with TestClient(create_app(isolated_services)) as client:
        api_response = client.get(
            f"/api/v1/tasks/{downstream.task_id}/call-graph",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert api_response.status_code == 200
    assert api_response.json()["root_task_id"] == upstream.task_id
    assert api_response.json()["root"]["task_id"] == upstream.task_id
    assert api_response.json()["root"]["created_at"]


def test_function_result_and_completion_reject_stale_container_attempt(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services.containers.scheduler = scheduler
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "stale-result-function",
        kind=StubKind.Function,
        handler="pkg.fn:handler",
        config={"runtime": {"image_id": "image-fn"}},
    )
    service = FunctionControlService(isolated_services)
    invoked = service.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=FunctionCloudpickleInvocation.from_bytes(
                cloudpickle_bytes({"args": (2,), "kwargs": {}})
            ),
        )
    )
    task = isolated_services.tasks.transition(
        isolated_services.tasks.get(invoked.task_id),
        TaskStatus.Running,
    )

    stored = service.function_set_result(
        FunctionSetResultBody(
            task_id=task.id,
            container_id="stale-container",
            result=FunctionCloudpickleResult.from_bytes(cloudpickle_bytes(4)),
        )
    )
    finished = service.finish_function_task(
        task.id,
        TaskStatus.Complete,
        container_id="stale-container",
        result=4,
        exit_code=0,
    )

    assert not stored.stored
    assert stored.status is TaskStatus.Running
    assert finished.status is TaskStatus.Running
    assert isolated_services.tasks.get(task.id).status is TaskStatus.Running
    assert isolated_services.tasks.get(task.id).function_result is None


def test_function_args_require_running_current_container_across_retry_replacement(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services.containers.scheduler = scheduler
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "function-args-fence",
        kind=StubKind.Function,
        handler="pkg.fn:handler",
        config={"runtime": {"image_id": "image-fn", "retries": 1}},
    )
    service = FunctionControlService(isolated_services)
    invocation = FunctionCloudpickleInvocation.from_bytes(
        cloudpickle_bytes({"args": (2,), "kwargs": {}})
    )
    invoked = service.function_invoke(FunctionInvokeBody(stub_id=stub.id, invocation=invocation))
    first_container = scheduler.requests[0].container_id

    with pytest.raises(ConflictError, match="pending"):
        service.function_get_args(
            FunctionGetArgsRequest(
                task_id=invoked.task_id,
                container_id=first_container,
            )
        )
    running = isolated_services.tasks.transition(
        isolated_services.tasks.get(invoked.task_id),
        TaskStatus.Running,
    )
    assert (
        service.function_get_args(
            FunctionGetArgsRequest(task_id=running.id, container_id=first_container)
        ).invocation
        == invocation
    )
    with pytest.raises(ConflictError, match="does not own"):
        service.function_get_args(
            FunctionGetArgsRequest(task_id=running.id, container_id="stale-container")
        )

    retry = service.finish_function_task(
        running.id,
        TaskStatus.Failed,
        container_id=first_container,
        error="retry",
        exit_code=1,
    )
    assert retry.status is TaskStatus.Retry
    assert len(scheduler.requests) == 1
    previous_container = isolated_services.containers.get(first_container)
    previous_container.status = ContainerStatus.Failed
    previous_container.finished_at = datetime.now(UTC)
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).records.upsert(
            previous_container,
            workspace_id=previous_container.workspace_id,
            name=previous_container.name,
            status=previous_container.status.value,
        )
    assert len(service.schedule_due_retries(now=datetime.now(UTC))) == 1
    assert len(scheduler.requests) == 2
    replacement_container = scheduler.requests[1].container_id
    assert replacement_container != first_container
    replacement = isolated_services.tasks.transition(
        isolated_services.tasks.get(running.id),
        TaskStatus.Running,
    )
    with pytest.raises(ConflictError, match="does not own"):
        service.function_get_args(
            FunctionGetArgsRequest(task_id=replacement.id, container_id=first_container)
        )
    assert (
        service.function_get_args(
            FunctionGetArgsRequest(
                task_id=replacement.id,
                container_id=replacement_container,
            )
        ).invocation
        == invocation
    )


def test_function_cancel_stops_container_and_rejects_terminal_writes(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    scheduler = _Scheduler()
    redis = real_redis_actors.client()
    services = replace(
        isolated_services,
        containers=replace(
            isolated_services.containers,
            scheduler=scheduler,
            scheduler_cancellation=scheduler_request_service_for_redis(
                isolated_services,
                redis,
            ),
        ),
    )
    stub = ControlPlaneService(services.context).create_stub(
        "cancelled-result-function",
        kind=StubKind.Function,
        handler="pkg.fn:handler",
        config={"runtime": {"image_id": "image-fn"}},
    )
    service = FunctionControlService(services)
    invoked = service.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=FunctionCloudpickleInvocation.from_bytes(
                cloudpickle_bytes({"args": (3,), "kwargs": {}})
            ),
        )
    )
    task = services.tasks.transition(
        services.tasks.get(invoked.task_id),
        TaskStatus.Running,
    )
    assert task.container_id is not None

    cancelled = service.cancel_task(task.id)
    stored = service.function_set_result(
        FunctionSetResultBody(
            task_id=task.id,
            container_id=task.container_id,
            result=FunctionCloudpickleResult.from_bytes(cloudpickle_bytes(9)),
        )
    )
    finished = service.finish_function_task(
        task.id,
        TaskStatus.Complete,
        container_id=task.container_id,
        result=9,
        exit_code=0,
    )

    assert cancelled.status is TaskStatus.Cancelled
    assert services.containers.get(task.container_id).status is ContainerStatus.Stopped
    assert not stored.stored
    assert stored.status is TaskStatus.Cancelled
    assert finished.status is TaskStatus.Cancelled
    assert services.tasks.get(task.id).status is TaskStatus.Cancelled
    assert services.tasks.get(task.id).function_result is None


def test_function_dependency_failure_fails_downstream_without_scheduling(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services.containers.scheduler = scheduler
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub(
        "nested-fn-failure",
        kind=StubKind.Function,
        handler="pkg.fn:handler",
        config={"runtime": {"image_id": "image-fn"}},
    )
    service = FunctionControlService(isolated_services)
    upstream = service.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=FunctionCloudpickleInvocation.from_bytes(
                cloudpickle_bytes({"args": (2,), "kwargs": {}})
            ),
        )
    )
    downstream = service.function_invoke(
        FunctionInvokeBody(
            stub_id=stub.id,
            invocation=FunctionCloudpickleInvocation.from_bytes(
                cloudpickle_bytes(
                    {
                        "args": ({FUNCTION_CALL_REF_MARKER: True, "task_id": upstream.task_id},),
                        "kwargs": {},
                    }
                )
            ),
            dependencies=[FunctionCallDependency(task_id=upstream.task_id)],
        )
    )
    upstream_task = isolated_services.tasks.get(upstream.task_id)

    failed = isolated_services.tasks.transition(
        upstream_task,
        TaskStatus.Failed,
        error="boom",
        exit_code=1,
    )
    service.release_dependents(failed)

    downstream_task = isolated_services.tasks.get(downstream.task_id)
    assert len(scheduler.requests) == 1
    assert downstream_task.status is TaskStatus.Failed
    assert downstream_task.container_id is None
    assert downstream_task.exit_code == 1
    assert downstream_task.error is not None
    assert upstream.task_id in downstream_task.error


def test_checkpoint_task_queue_runner_receives_checkpoint_barrier_env(
    isolated_services: ApiServices,
) -> None:
    scheduler = _Scheduler()
    isolated_services.containers.scheduler = scheduler
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "checkpoint-queue",
        kind=StubKind.TaskQueue,
        config={
            "image": {"image_id": "img_checkpoint_queue"},
            "runtime": {"checkpoint_enabled": True},
        },
    )

    TaskQueueControlService(
        isolated_services,
        redis=RedisClient(FakeRedis(), key_prefix="test"),
    ).start_task_queue_serve(StartTaskQueueServeRequest(stub_id=stub.id, timeout=30))

    payload = WorkerContainerRequestPayload.model_validate(scheduler.requests[0].payload)
    assert payload.checkpoint_enabled is True
    assert f"{CHECKPOINT_ENABLED_ENV}=true" in payload.env


class _Scheduler:
    def __init__(self) -> None:
        self.requests: list[SchedulerWorkerRequest] = []

    def submit(
        self,
        request: SchedulerWorkerRequest,
        *,
        ready_at: datetime | None = None,
    ) -> SchedulerContainerSubmitResult:
        _ = ready_at
        self.requests.append(request)
        return SchedulerContainerSubmitResult(
            status=SchedulerContainerSubmitStatus.Queued,
            container_id=request.container_id,
            reason="queued",
        )


class _RunningSchedulerContainers:
    def get_container_state(self, container_id: str) -> SchedulerContainerState:
        return SchedulerContainerState(
            container_id=container_id,
            stub_id="shell-stub",
            workspace_id="workspace-1",
            status=SchedulerContainerStatus.Running,
        )

    def get_worker_address(self, container_id: str) -> SchedulerContainerAddress | None:
        del container_id
        return None

    def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        return SchedulerContainerAddressMap(container_id=container_id)


def _usage_quantity(services: ApiServices, workspace_id: str, metric: UsageMetric) -> float:
    summary = services.usage.aggregate(query=UsageQuery(workspace_id=workspace_id))
    return {row.metric: row.quantity for row in summary}.get(metric, 0)
