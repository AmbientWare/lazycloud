import pytest
from database.context import ServiceContext
from gateway.events import GatewayRequestEventMiddleware
from httpx2 import ASGITransport, AsyncClient
from observability.events import EventService
from shared.events import EventLevel
from shared.worker_events import GATEWAY_REQUEST_EVENT_ACTION
from starlette.types import Receive, Scope, Send

from database import AsyncDatabaseClient


@pytest.mark.anyio
async def test_gateway_request_events_persist_server_errors(
    committed_service_context: ServiceContext,
) -> None:
    async def failing_app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 502, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    database = AsyncDatabaseClient.from_settings(committed_service_context.database.settings)
    events = EventService(committed_service_context, async_database=database)
    try:
        middleware = GatewayRequestEventMiddleware(failing_app, events)
        async with AsyncClient(
            transport=ASGITransport(app=middleware), base_url="http://testserver"
        ) as client:
            response = await client.get("/deploy-target")
    finally:
        await database.dispose()

    assert response.status_code == 502
    request_event = next(
        event for event in events.list() if event.action == GATEWAY_REQUEST_EVENT_ACTION
    )
    assert request_event.level is EventLevel.Error
    assert request_event.data["status_code"] == 502
    assert request_event.data["path"] == "/deploy-target"
