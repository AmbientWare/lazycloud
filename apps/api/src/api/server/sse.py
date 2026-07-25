from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse


def sse_event(event: str, event_id: str, payload: object) -> str:
    """Format one server-sent event frame."""
    data = json.dumps(jsonable_encoder(payload))
    identifier = f"id: {event_id}\n" if event_id else ""
    return f"{identifier}event: {event}\ndata: {data}\n\n"


def sse_response(items: Iterable[tuple[str, str, object]]) -> StreamingResponse:
    """Stream (event, id, payload) tuples as a server-sent-event response."""

    def events() -> Iterator[str]:
        yield ": connected\n\n"
        for event_name, event_id, payload in items:
            yield sse_event(event_name, event_id, payload)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["sse_event", "sse_response"]
