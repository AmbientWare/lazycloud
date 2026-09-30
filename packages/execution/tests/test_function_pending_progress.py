from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlServices
from database.records.apps import StubKind
from database.repositories.compute import (
    ComputeCapacityOperationRecord,
    ComputeCapacityOperationRepository,
    ComputeUnitRepository,
)
from database.repositories.execution import TaskRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import ContainerRepository
from execution.functions.service import FunctionControlService
from execution.task_progress import TaskProgressService
from observability.stream_state import AsyncTaskChangeReader
from shared.capacity import CapacityAcquisitionShape, CapacityFailureCode, CapacityOperationStatus
from shared.compute_policy import ComputeUnitRecord, UnitName
from shared.containers import ContainerRecord, ContainerStatus
from shared.function_payloads import FunctionJsonInvocation, FunctionJsonResult
from shared.http.functions import FunctionInvokeResponse
from shared.http.task_progress import TaskPendingProgress, TaskPendingReason
from shared.placement import Placement
from shared.scheduling import SchedulerContainerState
from shared.tasks import Task, TaskProgressSnapshot, TaskStatus
from shared.timestamps import utc_now


def test_capacity_diagnosis_survives_failover_until_worker_assignment(
    isolated_services: ApiServices,
) -> None:
    services = isolated_services
    now = utc_now()
    stub = ControlServices.create(services.context).stubs.create_stub(
        "capacity-progress", kind=StubKind.Function, handler="main:hello"
    )
    container = ContainerRecord(
        id=str(uuid4()),
        name="pending",
        image="python",
        command=[],
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        status=ContainerStatus.Pending,
    )
    task = services.tasks.create(
        "capacity-progress",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
        invocation=FunctionJsonInvocation(),
    )
    with services.context.database.session() as session:
        ContainerRepository(session).upsert(container)
        TaskRepository(session).mark_claimable(task.id, at=now)
        platform = WorkspaceRepository(session).create(name="capacity-owner")
        unit = ComputeUnitRepository(session).upsert(
            ComputeUnitRecord(
                id=str(uuid4()),
                workspace_id=platform.id,
                name=UnitName("capacity"),
                placement=Placement.platform(),
            )
        )
        for index, (demand, code) in enumerate(
            [
                (container.id, CapacityFailureCode.CapacityUnavailable),
                (container.id, CapacityFailureCode.ProviderQuotaExceeded),
                (container.id, None),
                ("another-workspace-container", CapacityFailureCode.ProviderLaunchFailed),
            ]
        ):
            operation_id = str(uuid4())
            ComputeCapacityOperationRepository(session).upsert(
                ComputeCapacityOperationRecord(
                    shape=CapacityAcquisitionShape(cpu_millicores=4_000, memory_mib=32_768),
                    id=operation_id,
                    workspace_id=platform.id,
                    pool_id=unit.id,
                    capacity_owner_id=unit.id,
                    reservation_id=str(uuid4()),
                    operation_id=operation_id,
                    demand_container_id=demand,
                    desired_unit=1,
                    status=CapacityOperationStatus.Released
                    if code
                    else CapacityOperationStatus.Requested,
                    failure_code=code,
                    last_error="provider detail must not reach the user",
                    created_at=now - timedelta(seconds=4 - index),
                )
            )
    task = services.tasks.get(task.id)
    state = SchedulerContainerState(
        container_id=container.id,
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    for reason in (TaskPendingReason.Queued, TaskPendingReason.ProvisioningCompute):
        state.pending_progress = TaskPendingProgress.for_reason(reason, since=now, observed_at=now)
        services.scheduler_containers.set_container_state(state)
        progress = services.tasks.progress.read([task])[task.id]
        assert progress is not None
        assert "provider compute quota exceeded" in progress.message
        assert "provider detail" not in progress.message
        assert progress.reason is (
            TaskPendingReason.CapacityUnavailable if reason is TaskPendingReason.Queued else reason
        )
    state.pending_progress = TaskPendingProgress.for_reason(
        TaskPendingReason.ProvisioningCompute,
        since=now - timedelta(minutes=1),
        observed_at=now - timedelta(minutes=1),
    )
    services.scheduler_containers.set_container_state(state)
    progress = services.tasks.progress.read([task])[task.id]
    assert progress is not None
    assert progress.reason is TaskPendingReason.CapacityUnavailable
    assert "provider compute quota exceeded" in progress.message
    assert "Starting" not in progress.message

    state.worker_id = str(uuid4())
    services.scheduler_containers.set_container_state(state)
    progress = services.tasks.progress.read([task])[task.id]
    assert progress is not None
    assert progress.reason is TaskPendingReason.StartingContainer
    assert "quota" not in progress.message


@pytest.mark.anyio
async def test_invocation_stream_updates_pending_reason_and_clears_on_completion(
    async_services: ApiServices,
) -> None:
    task = async_services.tasks.create("pending-stream", invocation=FunctionJsonInvocation())
    functions = FunctionControlService(
        async_services,
        async_database=async_services.require_async_io().database,
        task_changes=AsyncTaskChangeReader(async_services.require_async_io().realtime),
    )
    stream = functions.function_invoke_stream(FunctionInvokeResponse(task_id=task.id))
    try:
        async with asyncio.timeout(10):
            assert (await anext(stream)).task_id == task.id
            waiting = await anext(stream)
            assert waiting.status == TaskStatus.Pending.value
            assert waiting.pending_progress is not None
            assert waiting.pending_progress.reason is TaskPendingReason.Dependencies

            with async_services.context.database.session() as session:
                TaskRepository(session).upsert(task.model_copy(update={"claimable_at": utc_now()}))
            queued = await anext(stream)
            assert queued.status == waiting.status
            assert queued.pending_progress is not None
            assert queued.pending_progress.reason is TaskPendingReason.Queued

            async_services.tasks.transition(
                async_services.tasks.get(task.id),
                TaskStatus.Complete,
                function_result=FunctionJsonResult(value=7),
                exit_code=0,
            )
            async with asyncio.timeout(0.5):
                completed = await anext(stream)
            assert completed.status == TaskStatus.Complete.value
            assert completed.pending_progress is None
            assert (await anext(stream)).done
    finally:
        await stream.aclose()
    assert async_services.require_async_io().realtime.status().sources == 0


@pytest.mark.anyio
async def test_completion_does_not_wait_for_pending_progress(
    async_services: ApiServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = async_services.tasks.create("slow-progress", invocation=FunctionJsonInvocation())
    entered = Event()
    release = Event()
    original = TaskProgressService.read

    def delayed(
        owner: TaskProgressService, tasks: Sequence[Task | TaskProgressSnapshot]
    ) -> dict[str, TaskPendingProgress | None]:
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError("progress test did not release its reader")
        return original(owner, tasks)

    monkeypatch.setattr(TaskProgressService, "read", delayed)
    functions = FunctionControlService(
        async_services,
        async_database=async_services.require_async_io().database,
        task_changes=AsyncTaskChangeReader(async_services.require_async_io().realtime),
    )

    async def consume() -> list[FunctionInvokeResponse]:
        return [
            item
            async for item in functions.function_invoke_stream(
                FunctionInvokeResponse(task_id=task.id)
            )
        ]

    stream = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        async_services.tasks.transition(
            task, TaskStatus.Complete, function_result=FunctionJsonResult(value=7), exit_code=0
        )
        responses = await asyncio.wait_for(stream, timeout=0.5)
        assert responses[-1].done
        assert responses[-1].result == FunctionJsonResult(value=7)
    finally:
        release.set()
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)


@pytest.mark.anyio
async def test_completed_invocation_drains_all_log_pages(async_services: ApiServices) -> None:
    task = async_services.tasks.create("completed-stream", invocation=FunctionJsonInvocation())
    messages = [f"line {index}" for index in range(1_001)]
    async_services.tasks.append_logs(task.id, "stdout", messages)
    async_services.tasks.transition(
        task, TaskStatus.Complete, function_result=FunctionJsonResult(value=7), exit_code=0
    )
    functions = FunctionControlService(
        async_services,
        async_database=async_services.require_async_io().database,
        task_changes=AsyncTaskChangeReader(async_services.require_async_io().realtime),
    )
    async with asyncio.timeout(5):
        responses = [
            response
            async for response in functions.function_invoke_stream(
                FunctionInvokeResponse(task_id=task.id)
            )
        ]
    assert [response.output.rstrip("\n") for response in responses if response.output] == messages
    assert responses[-1].done
    assert responses[-1].result == FunctionJsonResult(value=7)
    assert async_services.require_async_io().realtime.status().sources == 0
