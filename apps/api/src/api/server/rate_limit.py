from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from coordination.rate_limit import try_consume
from coordination.redis_client import RedisClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.server.client_address import client_address

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UnauthenticatedRouteLimit:
    """One prefix's budget, per calling address and across all of them."""

    prefix: str
    per_address_per_minute: int
    global_per_minute: int
    # Empty means every method. Naming one keeps a budget meant for guessing a
    # credential off the reads that share its prefix: /api/v1/sessions/current is
    # fetched on every dashboard mount, and spending the anti-stuffing budget on it
    # would let ordinary authenticated traffic refuse everyone else's sign-in.
    methods: frozenset[str] = frozenset()
    # A limiter that cannot reach Redis either lets everything through or
    # nothing. For the routes that gate the platform's own liveness probes,
    # everything is the safer answer; for the routes an attacker can reach
    # without credentials, nothing is.
    fail_open: bool = False


DEFAULT_UNAUTHENTICATED_LIMITS: tuple[UnauthenticatedRouteLimit, ...] = (
    UnauthenticatedRouteLimit("/gateway/provider-nodes/", 30, 600),
    UnauthenticatedRouteLimit("/auth/github/start", 10, 200),
    # Looser than the rest: a shared NAT can put a great many legitimate people
    # behind one address, and every one of them lands here within a minute of
    # each other.
    UnauthenticatedRouteLimit("/auth/github/callback", 20, 400),
    UnauthenticatedRouteLimit("/auth/device", 10, 200),
    UnauthenticatedRouteLimit("/auth/authorize", 10, 200),
    # Redeeming a sign-in code. The budget bounds abuse of the exchange rather than
    # guessing: the code and the cookie nonce are each 256 bits of urandom, and the
    # code is single-use. The global cap matters as much as the per-address one,
    # because anything worth doing here is spread across many addresses.
    UnauthenticatedRouteLimit("/api/v1/sessions", 10, 200, methods=frozenset({"POST"})),
    UnauthenticatedRouteLimit("/health", 60, 600, fail_open=True),
    # Payment outcomes. Generous per address because they all arrive from the
    # provider's own small set of egress addresses, and a busy close can deliver
    # a burst; bounded because this is a public POST that does an HMAC and a
    # database write before it can tell a real delivery from noise.
    UnauthenticatedRouteLimit("/webhooks/", 120, 2_000, methods=frozenset({"POST"})),
)

_WINDOW_SECONDS = 60
_RETRY_AFTER = str(_WINDOW_SECONDS)


@dataclass(slots=True)
class UnauthenticatedRateLimitMiddleware:
    """Refuse excess unauthenticated traffic before it reaches a route.

    Sits inside the request-event middleware so a refusal is still metered as
    a 4xx — the durable event writer only persists 5xx, so a refused request
    is counted without becoming a database write.
    """

    app: ASGIApp
    # Resolved per request: services are published on app state at startup,
    # after middleware is registered.
    redis: Callable[[], RedisClient]
    client_ip_header: str = ""
    limits: tuple[UnauthenticatedRouteLimit, ...] = field(default=DEFAULT_UNAUTHENTICATED_LIMITS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(
            str(scope.get("path") or "/"),
            str(scope.get("method") or ""),
        )
        if limit is not None and not self._admit(scope, limit):
            await _refuse(send)
            return
        await self.app(scope, receive, send)

    def _limit_for(self, path: str, method: str) -> UnauthenticatedRouteLimit | None:
        for limit in self.limits:
            if path.startswith(limit.prefix) and (not limit.methods or method in limit.methods):
                return limit
        return None

    def _admit(self, scope: Scope, limit: UnauthenticatedRouteLimit) -> bool:
        # An unresolvable address shares one bucket rather than escaping the
        # limiter entirely.
        address = client_address(scope, header_name=self.client_ip_header) or "unknown"
        try:
            redis = self.redis()
            if not try_consume(
                redis,
                f"ratelimit:{limit.prefix}:addr:{address}",
                limit=limit.per_address_per_minute,
                window_seconds=_WINDOW_SECONDS,
            ):
                return False
            return try_consume(
                redis,
                f"ratelimit:{limit.prefix}:global",
                limit=limit.global_per_minute,
                window_seconds=_WINDOW_SECONDS,
            )
        except Exception:
            logger.exception(
                "rate limiter unavailable for %s; failing %s",
                limit.prefix,
                "open" if limit.fail_open else "closed",
            )
            return limit.fail_open


async def _refuse(send: Send) -> None:
    start: Message = {
        "type": "http.response.start",
        "status": 429,
        "headers": [
            (b"content-type", b"application/json"),
            (b"retry-after", _RETRY_AFTER.encode()),
        ],
    }
    await send(start)
    await send({"type": "http.response.body", "body": b'{"detail":"too many requests"}'})
