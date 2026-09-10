from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.execution import TaskRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from observability.container_logs import (
    ContainerLogIngestionService,
    ContainerLogMachineAssignmentError,
    ContainerLogRuntimeAttribution,
    ContainerLogWorkerAssignmentError,
)
from observability.stream_state import (
    RedisEventStreamRepository,
)
from pydantic import JsonValue, TypeAdapter
from shared.compute_fleet import Machine, Worker
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import DeploymentSpec
from shared.errors import ConflictError
from shared.realtime.streams import LogStreamQuery
from shared.tasks import Task

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def test_container_log_ingestion_service_supports_direct_durable_attribution(
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
                id=str(uuid4()),
                name="direct-container-log",
                image="img-direct-container-log",
                command=["python3.12", "-m", "runner.serve"],
                workspace_id=workspace_id,
                machine_id=machine.id,
                worker_id=worker.id,
                status=ContainerStatus.Running,
            )
        )
    repository = RedisEventStreamRepository(isolated_services.redis())
    service = ContainerLogIngestionService(isolated_services.context, repository)
    entries = [
        _Entry(sequence=0, message="root stdout"),
        _Entry(sequence=1, message="root stderr", stream="stderr"),
    ]

    result = service.append_batch(
        container_id=container.id,
        capture_id="direct-capture",
        entries=entries,
    )

    captured = repository.read_logs(
        LogStreamQuery(workspace_id=workspace_id, object_type="container", object_id=container.id)
    )
    assert len(captured) == 2
    assert result.accepted_through == 1
    assert result.appended_count == 2
    assert [event.body["workspaceid"] for event in captured] == [
        workspace_id,
        workspace_id,
    ]
    assert [event.body["workerid"] for event in captured] == [worker.id, worker.id]
    assert [event.body["machineid"] for event in captured] == [machine.id, machine.id]
    first_data = _JSON_OBJECT_ADAPTER.validate_python(captured[0].body["data"])
    second_data = _JSON_OBJECT_ADAPTER.validate_python(captured[1].body["data"])
    assert first_data["source_sequence"] == 0
    assert second_data["stream"] == "stderr"

    with pytest.raises(ContainerLogWorkerAssignmentError, match="assigned worker"):
        service.append_batch(
            container_id=container.id,
            capture_id="wrong-worker",
            entries=[_Entry(sequence=0, message="rejected")],
            expected_worker_id=str(uuid4()),
        )

    with pytest.raises(ContainerLogWorkerAssignmentError, match="durable assignment"):
        service.append_runtime_batch(
            container_id=container.id,
            capture_id="wrong-runtime-worker",
            entries=[_Entry(sequence=0, message="rejected")],
            attribution=ContainerLogRuntimeAttribution(
                worker_id=str(uuid4()),
                machine_id=machine.id,
            ),
        )

    with pytest.raises(ContainerLogMachineAssignmentError, match="durable assignment"):
        service.append_runtime_batch(
            container_id=container.id,
            capture_id="wrong-runtime-machine",
            entries=[_Entry(sequence=0, message="rejected")],
            attribution=ContainerLogRuntimeAttribution(
                worker_id=worker.id,
                machine_id=str(uuid4()),
            ),
        )


def test_container_log_runtime_attribution_uses_durable_ownership(
    isolated_services: ApiServices,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(name="runtime-container-log", handler="pkg.module:handler")
    )
    stub = next(
        item
        for item in ControlPlaneService(isolated_services.context).list_stubs()
        if item.deployment_id == deployment.id
    )
    runtime_worker_id = "compose-container-worker"
    runtime_machine_id = "compose-machine"
    container = ContainerRecord(
        id=str(uuid4()),
        name="runtime-container-log",
        image="img-runtime-container-log",
        command=["python3.12", "-m", "runner.serve"],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        app_id=stub.app_id,
        runtime_worker_id=runtime_worker_id,
        runtime_machine_id=runtime_machine_id,
        status=ContainerStatus.Running,
    )
    with isolated_services.context.database.session() as session:
        container = ContainerRepository(session).upsert(container)
    task = isolated_services.tasks.create(
        "runtime-container-log",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        app_id=stub.app_id,
        deployment_id=deployment.id,
        container_id=container.id,
    )
    with isolated_services.context.database.session() as session:
        container = ContainerRepository(session).upsert(
            container.model_copy(update={"task_id": task.id})
        )

    repository = RedisEventStreamRepository(isolated_services.redis())
    service = ContainerLogIngestionService(isolated_services.context, repository)
    with pytest.raises(ConflictError, match="durable worker assignment"):
        service.append_batch(
            container_id=container.id,
            capture_id="repository-capture",
            entries=[_Entry(sequence=0, message="rejected")],
        )

    result = service.append_runtime_batch(
        container_id=container.id,
        capture_id="runtime-capture",
        entries=[_Entry(sequence=0, message="root stdout")],
        attribution=ContainerLogRuntimeAttribution(
            worker_id=runtime_worker_id,
            machine_id=runtime_machine_id,
        ),
    )

    captured = repository.read_logs(
        LogStreamQuery(
            workspace_id=stub.workspace_id, object_type="container", object_id=container.id
        )
    )
    assert len(captured) == 1
    assert result.accepted_through == 0
    assert result.appended_count == 1
    assert captured[0].body["workspaceid"] == stub.workspace_id
    assert captured[0].body["stubid"] == stub.id
    assert captured[0].body["appid"] == stub.app_id
    assert captured[0].body["taskid"] == task.id
    assert captured[0].body["workerid"] == runtime_worker_id
    assert captured[0].body["machineid"] == runtime_machine_id

    with pytest.raises(ContainerLogWorkerAssignmentError, match="execution assignment"):
        service.append_runtime_batch(
            container_id=container.id,
            capture_id="wrong-runtime-worker",
            entries=[_Entry(sequence=0, message="rejected")],
            attribution=ContainerLogRuntimeAttribution(
                worker_id="other-compose-worker",
                machine_id=runtime_machine_id,
            ),
        )
    with pytest.raises(ContainerLogMachineAssignmentError, match="execution assignment"):
        service.append_runtime_batch(
            container_id=container.id,
            capture_id="wrong-runtime-machine",
            entries=[_Entry(sequence=0, message="rejected")],
            attribution=ContainerLogRuntimeAttribution(
                worker_id=runtime_worker_id,
                machine_id="other-compose-machine",
            ),
        )


def test_container_log_runtime_attribution_rejects_durable_task_conflict(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        other_workspace = WorkspaceRepository(session).create(name="other-log-owner")
        task = TaskRepository(session).upsert(
            Task(
                id=str(uuid4()),
                name="other-workspace-task",
                workspace_id=other_workspace.id,
            )
        )
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="runtime-task-conflict",
                image="img-runtime-task-conflict",
                command=["true"],
                workspace_id=workspace_id,
                task_id=task.id,
                status=ContainerStatus.Running,
            )
        )
    service = ContainerLogIngestionService(
        isolated_services.context,
        RedisEventStreamRepository(isolated_services.redis()),
    )

    with pytest.raises(ConflictError, match="task workspace"):
        service.append_runtime_batch(
            container_id=container.id,
            capture_id="runtime-task-conflict",
            entries=[_Entry(sequence=0, message="rejected")],
            attribution=ContainerLogRuntimeAttribution(worker_id="compose-container-worker"),
        )


@dataclass(frozen=True, slots=True)
class _Entry:
    sequence: int
    message: str
    stream: str = "stdout"
    timestamp: datetime = datetime(2026, 7, 16, 18, 0, tzinfo=UTC)
    kind: str = "output"
    dropped_count: int = 0
