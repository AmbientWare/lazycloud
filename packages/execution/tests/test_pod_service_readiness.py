from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from coordination.redis_client import RedisClient
from database.repositories.execution import PodExecutionRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from execution.pods.service import PodControlService
from execution.services import ExecutionServices
from shared.compute_fleet import Machine, Worker
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError, NotFoundError
from shared.http.pods import PodSandboxExposePortRequest
from shared.scheduling import (
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
    SchedulerContainerState,
)
from tests.redis_fakes import FakeRedis

CONTAINER_ID = "00000000-0000-4000-8000-000000000101"


@dataclass(frozen=True, slots=True)
class _ExposedPortsResponse:
    ports: tuple[int, ...]
    ok: bool = True
    error_msg: str = ""


@dataclass(frozen=True, slots=True)
class _ExposedPortsClient:
    ports: tuple[int, ...]

    def sandbox_list_exposed_ports(self, container_id: str) -> _ExposedPortsResponse:
        _ = container_id
        return _ExposedPortsResponse(ports=self.ports)


@dataclass(slots=True)
class _TerminalTransitionRepository:
    services: ExecutionServices
    calls: int = 0

    def get_container_state(self, container_id: str) -> SchedulerContainerState | None:
        self.calls += 1
        if self.calls == 1:
            with self.services.context.database.session() as session:
                repository = ContainerRepository(session)
                container = repository.get_across_workspaces(container_id)
                assert container is not None
                container.status = ContainerStatus.Failed
                container.exit_code = 1
                repository.upsert(container)
        return None

    def get_worker_address(self, container_id: str) -> SchedulerContainerAddress | None:
        del container_id
        return None

    def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        raise AssertionError(f"address map should not be read for terminal {container_id}")


def test_wait_for_container_client_reloads_durable_terminal_state(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    container = ContainerRecord(
        id=CONTAINER_ID,
        name="sandbox-failed",
        image="image",
        command=["sleep", "300"],
        workspace_id=workspace_id,
        stub_id="sandbox-stub",
    )
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(container)

    scheduler = _TerminalTransitionRepository(isolated_services)
    service = PodControlService(
        isolated_services,
        scheduler_containers=scheduler,
        poll_interval_seconds=0,
        redis=RedisClient(FakeRedis(), key_prefix="test"),
    )

    with pytest.raises(ConflictError, match=f"container {CONTAINER_ID} is failed"):
        service._wait_for_container_client(container, timeout_seconds=1)

    assert scheduler.calls == 1


def test_mark_container_running_preserves_compute_foreign_keys_and_runtime_assignment(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        machine = MachineRepository(session).upsert(
            Machine(id=str(uuid4())),
            workspace_id=workspace_id,
        )
        worker = WorkerRepository(session).upsert(
            Worker(id=str(uuid4()), machine_id=machine.id),
            workspace_id=workspace_id,
        )
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id="00000000-0000-4000-8000-000000000102",
                name="managed-runtime-assignment",
                image="image",
                command=["sleep", "300"],
                workspace_id=workspace_id,
                machine_id=machine.id,
                worker_id=worker.id,
                runtime_machine_id="compose-machine",
                runtime_worker_id="compose-container-worker",
            )
        )
    service = PodControlService(isolated_services, redis=isolated_services.redis())

    service._mark_container_running(
        container,
        SchedulerContainerState(
            container_id=container.id,
            workspace_id=workspace_id,
            stub_id="",
            worker_id="compose-container-worker",
        ),
    )

    with isolated_services.context.database.session() as session:
        updated = ContainerRepository(session).get_across_workspaces(container.id)
    assert updated is not None
    assert updated.status is ContainerStatus.Running
    assert updated.worker_id == worker.id
    assert updated.machine_id == machine.id
    assert updated.runtime_worker_id == "compose-container-worker"
    assert updated.runtime_machine_id == "compose-machine"


def test_sandbox_exposure_rejects_cross_workspace_stub_before_worker_callback(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    control.upsert_workspace("other-workspace")
    foreign_stub = control.create_stub(
        "foreign-sandbox",
        workspace="other-workspace",
        kind=StubKind.Sandbox,
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id="00000000-0000-4000-8000-000000000103",
                name="cross-workspace-sandbox",
                image="image",
                command=["sleep", "300"],
                workspace_id=workspace_id,
                stub_id=foreign_stub.id,
                status=ContainerStatus.Running,
            )
        )
    callback_called = False

    def reject_worker_callback(
        service: PodControlService,
        container_id: str,
    ) -> None:
        nonlocal callback_called
        _ = service, container_id
        callback_called = True
        raise AssertionError("worker callback must not run for invalid ownership")

    monkeypatch.setattr(PodControlService, "_client", reject_worker_callback)
    service = PodControlService(isolated_services, redis=isolated_services.redis())

    with pytest.raises(NotFoundError):
        service.sandbox_expose_port(
            container.id,
            PodSandboxExposePortRequest(port=8080),
        )

    assert callback_called is False
    with isolated_services.context.database.session() as session:
        assert (
            PodExecutionRepository(session).urls.get(container_id=container.id, port=8080) is None
        )


def test_stub_visibility_mutation_rewrites_only_its_persisted_sandbox_urls(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    stub = control.create_stub("stub-visibility", kind=StubKind.Sandbox)
    peer = control.create_stub("stub-visibility-peer", kind=StubKind.Sandbox)
    with isolated_services.context.database.session() as session:
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id="00000000-0000-4000-8000-000000000107",
                name="stub-visibility",
                image="image",
                command=["sleep", "300"],
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                status=ContainerStatus.Running,
            )
        )
        peer_container = ContainerRepository(session).upsert(
            ContainerRecord(
                id="00000000-0000-4000-8000-000000000108",
                name="stub-visibility-peer",
                image="image",
                command=["sleep", "300"],
                workspace_id=peer.workspace_id,
                stub_id=peer.id,
                status=ContainerStatus.Running,
            )
        )
        for selected in (container, peer_container):
            PodExecutionRepository(session).urls.upsert(
                container_id=selected.id,
                port=8080,
                url=f"http://127.0.0.1:9000/sandbox/id/{selected.id}/8080",
            )

    control.create_stub("stub-visibility", kind=StubKind.Sandbox, public=True)
    with isolated_services.context.database.session() as session:
        stored = PodExecutionRepository(session).urls.get(container_id=container.id, port=8080)
        peer_stored = PodExecutionRepository(session).urls.get(
            container_id=peer_container.id,
            port=8080,
        )
    assert stored is not None and "/sandbox/public/" in stored.url
    assert peer_stored is not None and "/sandbox/id/" in peer_stored.url

    control.create_stub("stub-visibility", kind=StubKind.Sandbox, public=False)
    with isolated_services.context.database.session() as session:
        private = PodExecutionRepository(session).urls.get(container_id=container.id, port=8080)
    assert private is not None and "/sandbox/id/" in private.url
