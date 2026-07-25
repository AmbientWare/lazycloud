"""Small ASGI wire types shared by the runnable examples."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Required, TypedDict


class ASGIMessage(TypedDict, total=False):
    type: Required[str]
    path: str
    query_string: bytes
    subprotocols: list[str]
    text: str | None
    bytes: bytes | None
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes
    more_body: bool
    subprotocol: str | None
    code: int
    reason: str


type ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
type ASGISend = Callable[[ASGIMessage], Awaitable[None]]


__all__ = ["ASGIMessage", "ASGIReceive", "ASGISend"]
