from __future__ import annotations

import asyncio

import pytest
from api.server.services import ApiServices
from database.repositories.execution import TaskRepository
from execution.functions.service import FunctionControlService
from shared.function_payloads import FunctionJsonInvocation, FunctionJsonResult
from shared.http.functions import FunctionInvokeResponse
from shared.http.task_progress import TaskPendingReason
from shared.tasks import TaskStatus
from shared.timestamps import utc_now


@pytest.mark.anyio
async def test_invocation_stream_updates_pending_reason_and_clears_on_completion(
    async_services: ApiServices,
) -> None:
    task = async_services.tasks.create("pending-stream", invocation=FunctionJsonInvocation())
    functions = FunctionControlService(
        async_services, async_database=async_services.require_async_io().database
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
            completed = await anext(stream)
            assert completed.status == TaskStatus.Complete.value
            assert completed.pending_progress is None
            assert (await anext(stream)).done
    finally:
        await stream.aclose()
