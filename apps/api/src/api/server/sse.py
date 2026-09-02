from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable

from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

type SseItem = tuple[str, str, object] | None

SSE_HEARTBEAT_SECONDS = 15.0
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def sse_event(event: str, event_id: str, payload: object) -> str:
    identifier = f"id: {event_id}\n" if event_id else ""
    data = json.dumps(jsonable_encoder(payload))
    return f"{identifier}event: {event}\ndata: {data}\n\n"


async def _sse_events(items: AsyncIterable[SseItem]) -> AsyncIterator[str]:
    yield ": connected\n\n"
    async for item in items:
        if item is None:
            yield ": heartbeat\n\n"
            continue
        event_name, event_id, payload = item
        yield sse_event(event_name, event_id, payload)


class _PreparedSseResponse(StreamingResponse):
    """An SSE response that opens its subscription when the response runs.

    A sync handler cannot await the subscription, and opening it inside the
    body generator would come after the status line, where a failure can only
    end the stream. Opening it here still reaches the error handlers and
    answers with a status.
    """

    def __init__(
        self,
        prepare_items: Callable[[], Awaitable[AsyncIterable[SseItem]]],
    ) -> None:
        self._prepare_items = prepare_items
        super().__init__((), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.body_iterator = _sse_events(await self._prepare_items())
        await super().__call__(scope, receive, send)


def sse_response(items: AsyncIterable[SseItem]) -> StreamingResponse:
    """Stream (event, id, payload) tuples as a server-sent-event response."""

    return StreamingResponse(
        _sse_events(items),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def sse_response_prepared(
    prepare_items: Callable[[], Awaitable[AsyncIterable[SseItem]]],
) -> StreamingResponse:
    return _PreparedSseResponse(prepare_items)


def sse_response_items(items: Iterable[tuple[str, str, object]]) -> StreamingResponse:
    async def async_items() -> AsyncIterator[tuple[str, str, object]]:
        for item in items:
            yield item

    return sse_response(async_items())


__all__ = [
    "SSE_HEARTBEAT_SECONDS",
    "SseItem",
    "sse_event",
    "sse_response",
    "sse_response_items",
    "sse_response_prepared",
]
