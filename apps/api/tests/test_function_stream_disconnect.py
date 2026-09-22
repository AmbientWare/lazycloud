from __future__ import annotations

import asyncio

import pytest
from api.server.routers.functions import _FunctionInvokeStreamResponse
from api.server.services import ApiServices
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import FunctionInvokeResponse
from shared.tasks import TaskStatus
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope


@pytest.mark.anyio
@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
@pytest.mark.parametrize("headless", [False, True])
async def test_disconnected_invocation_cancels_only_attached_task(
    async_services: ApiServices, spec_version: str, headless: bool
) -> None:
    task = async_services.tasks.create("disconnected", invocation=FunctionJsonInvocation())
    neighbour = async_services.tasks.create("other-caller", invocation=FunctionJsonInvocation())
    response = _FunctionInvokeStreamResponse(
        async_services.function_service,
        FunctionInvokeResponse(task_id=task.id),
        headless=headless,
    )
    sent = asyncio.Event()
    bodies = 0

    async def receive() -> Message:
        await sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal bodies
        if message["type"] == "http.response.body":
            bodies += 1
            if headless or bodies == 2:
                sent.set()
                if spec_version == "2.4":
                    raise OSError("client connection closed")

    scope: Scope = {"type": "http", "asgi": {"spec_version": spec_version}}
    if spec_version == "2.4":
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
    else:
        await response(scope, receive, send)

    assert async_services.tasks.get(task.id).status is (
        TaskStatus.Pending if headless else TaskStatus.Cancelled
    )
    assert async_services.tasks.get(neighbour.id).status is TaskStatus.Pending
    assert async_services.require_async_io().realtime.status().sources == 0
