from __future__ import annotations

from datetime import datetime

from control.service import StubKind
from operations.management import ManagementService
from shared.errors import InvalidInputError

from api.server.services import ApiServices

STUB_TYPE_ALIASES = {
    "taskqueue": StubKind.TaskQueue,
    "task-queue": StubKind.TaskQueue,
    "endpoint": StubKind.Endpoint,
    "http": StubKind.Endpoint,
    "asgi": StubKind.Asgi,
    "function": StubKind.Function,
    "pod": StubKind.Pod,
    "sandbox": StubKind.Sandbox,
}


def _management(services: ApiServices) -> ManagementService:
    return ManagementService(services)


def _parsed_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"invalid timestamp: {value}"
        raise InvalidInputError(msg) from exc
