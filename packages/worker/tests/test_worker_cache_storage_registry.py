from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from scheduler.state import RedisSchedulerContainerRepository
from shared.compute_policy import MachinePool
from shared.routing import BackendRouteKind, BackendRouteState
from shared.scheduling import (
    SchedulerWorkerStatus,
    WorkerExecutionRecord,
    WorkerRemovalResult,
    WorkerUnavailableReason,
)
from tests.real_redis import RealRedisActors
from worker.adapters import WorkerRouteIdentity, WorkerRouteRecovery
from worker.container_service.models import WorkerContainerServiceInstance
from worker.container_service.state import LocalWorkerContainerInstanceStore
from worker.worker_lifecycle import (
    WorkerLifecycleOrchestrator,
)

_CAPACITY_OWNER_ID = "11111111-1111-4111-8111-111111111111"


def test_pending_worker_restores_live_routes_before_registration_recovers(
    real_redis_actors: RealRedisActors,
) -> None:
    worker = WorkerExecutionRecord(
        worker_id="worker-1",
        machine_id="machine-1",
        pool=MachinePool("default"),
        capacity_owner_id=_CAPACITY_OWNER_ID,
        status=SchedulerWorkerStatus.Pending,
    )
    repo = _FakeLifecycleRepo(worker=worker)
    containers = RedisSchedulerContainerRepository(real_redis_actors.client())
    instances = LocalWorkerContainerInstanceStore()
    live = WorkerContainerServiceInstance(
        container_id="live-container",
        root_path="/containers/live",
        workspace_id="workspace-1",
        worker_id=worker.worker_id,
        machine_id=worker.machine_id,
        pool=worker.pool,
        address_map={8001: "", 8080: "192.168.0.2:8080"},
    )
    instances.save_container_instance(live)
    instances.save_container_instance(live.model_copy(update={"container_id": "stopped-container"}))
    instances.save_container_instance(
        WorkerContainerServiceInstance(
            container_id="active-build",
            root_path="",
            workspace_id="workspace-1",
            build_request=True,
            status="running",
        )
    )
    recovery = WorkerRouteRecovery(
        identity=WorkerRouteIdentity(
            worker_id=worker.worker_id,
            machine_id=worker.machine_id,
            pool=worker.pool,
            pod_address="127.0.0.1",
            container_service_port=9001,
        ),
        containers=containers,
        instances=instances,
        runtime=_LocalRuntime(),
    )
    lifecycle = WorkerLifecycleOrchestrator(
        worker_id=worker.worker_id,
        repository=repo,
        registration=worker,
        route_restorer=recovery.restore,
    )

    failed = lifecycle.keepalive()
    assert not failed.ok
    assert "empty local route target" in failed.error_message
    assert repo.worker is not None and repo.worker.status is SchedulerWorkerStatus.Pending

    live.address_map[8001] = "192.168.0.2:8001"
    result = lifecycle.keepalive()

    assert result.ok
    assert result.metadata == {"re_registered": "true"}
    assert repo.worker is not None
    assert repo.worker.status is SchedulerWorkerStatus.Available
    restored = containers.get_container_address_map(live.container_id)
    assert restored.address_map == live.address_map
    assert {route.port for route in restored.routes} == {8001, 8080}
    assert all(route.state is BackendRouteState.Opening for route in restored.routes)
    worker_address = containers.get_worker_address(live.container_id)
    assert worker_address is not None
    worker_route = worker_address.route
    assert worker_route is not None and worker_route.kind is BackendRouteKind.Worker
    assert worker_route.local_target == "127.0.0.1:9001"
    build_address = containers.get_worker_address("active-build")
    assert build_address is not None and build_address.route is not None
    assert containers.get_worker_address("stopped-container") is None
    assert containers.get_container_address_map("stopped-container").routes == []


class _LocalRuntime:
    def status(self, container_id: str) -> str:
        if container_id == "live-container":
            return "running"
        if container_id == "stopped-container":
            return "stopped"
        raise AssertionError("Image builds are owned by the local build instance")


@dataclass(slots=True)
class _FakeLifecycleRepo:
    actions: list[str] = field(default_factory=list)
    registered: list[WorkerExecutionRecord] = field(default_factory=list)
    fail_next_keepalive: bool = False
    worker: WorkerExecutionRecord | None = None

    def add_worker(
        self,
        worker: WorkerExecutionRecord,
        *,
        ttl_seconds: int = 0,
        now: datetime | None = None,
    ) -> WorkerExecutionRecord:
        _ = ttl_seconds, now
        self.actions.append("registered")
        self.registered.append(worker)
        self.worker = worker
        return worker

    def toggle_worker_available(
        self,
        worker_id: str,
        *,
        ttl_seconds: int,
    ) -> WorkerExecutionRecord | None:
        _ = worker_id, ttl_seconds
        self.actions.append("available")
        if self.worker is not None:
            self.worker = self.worker.model_copy(update={"status": SchedulerWorkerStatus.Available})
        return self.worker

    def set_keep_alive(
        self,
        worker_id: str,
        *,
        ttl_seconds: int,
    ) -> WorkerExecutionRecord | None:
        _ = ttl_seconds
        self.actions.append("keepalive")
        if self.fail_next_keepalive:
            self.fail_next_keepalive = False
            msg = f"worker {worker_id!r} state is missing"
            raise RuntimeError(msg)
        return self.worker

    def prepare_source_cache(self) -> None:
        self.actions.append("activated")

    def disable_worker(
        self,
        worker_id: str,
        *,
        reason: WorkerUnavailableReason,
        detail: str = "",
        ttl_seconds: int,
    ) -> None:
        _ = worker_id, reason, detail, ttl_seconds
        self.actions.append("disabled")

    def remove_worker(self, worker_id: str) -> WorkerRemovalResult:
        self.actions.append("removed")
        return WorkerRemovalResult(worker_id=worker_id, removed=True)
