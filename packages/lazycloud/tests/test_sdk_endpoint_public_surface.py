from __future__ import annotations

from collections.abc import Awaitable, Iterator
from typing import Any

import pytest
from lazycloud.abstractions.endpoint import (
    ASGIMessage,
    ASGIReceive,
    ASGISend,
)
from shared.env import CONTAINER_ID_ENV

FUNCTION_ONLY_METHODS = (
    "remote",
    "run",
    "spawn",
    "spawn_map",
    "async_remote",
    "async_spawn",
    "map",
)
CONTROL_PLUMBING_OPTIONS = {
    "stub_id",
    "client",
    "deployment_client",
    "container_client",
    "gateway_client",
    "workspace",
    "endpoint",
    "token",
    "terminal",
    "sync_local_dir",
    "client_timeout_seconds",
    "deployment_id",
    "image_id",
    "checkpoint_id",
}
FUNCTION_UNSUPPORTED_OPTIONS = {
    "workers",
    "concurrency",
    "keep_warm",
    "max_pending_tasks",
    "autoscaler",
    "checkpoint_enabled",
    "tcp",
    "block_network",
    "allow_list",
    "ports",
}
NON_POD_NETWORK_OPTIONS = {"tcp", "block_network", "allow_list", "ports"}


def _clear_container_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONTAINER_ID_ENV, raising=False)


async def _empty_receive() -> ASGIMessage:
    return {"type": "http.request", "body": b"", "more_body": False}


def _websocket_receive(messages: list[ASGIMessage]) -> ASGIReceive:
    pending: Iterator[ASGIMessage] = iter(messages)

    async def receive() -> ASGIMessage:
        return next(pending, {"type": "websocket.disconnect"})

    return receive


def _async_append(target: list[ASGIMessage]) -> ASGISend:
    async def append(message: ASGIMessage) -> None:
        target.append(message)

    return append


async def _await_result(awaitable: Awaitable[Any]) -> None:
    await awaitable
