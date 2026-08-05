"""One HTTP client for every internal hop, addressed by peer name.

Before this existed the platform dialed internal services from seven independent
places across three stacks — `http.client`, `urllib.request`, and `httpx` — each
deriving its connection from `urlparse(url).hostname`. That coupling is why a
control-plane origin only the control plane could resolve reached a remote
machine and failed there rather than at startup: there was no single place that
could decide how a destination should be reached.

The transport is chosen by the destination itself. A host inside the tailnet is
resolved to a peer and dialed at its address; anything else is dialed as written.
That is a property of the address, not a mode the process is in, so a local
Compose service name and a remote peer take the same code path.

The substitution happens at the address only. The request keeps the `Host` its
URL named, and TLS keeps the original name for SNI and certificate validation.
That matters beyond tidiness: object-store URLs are signed with SigV4, which
covers `Host`, so a client that rewrote the URL to reach a peer would invalidate
every signature it touched.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import IO, Protocol
from urllib.parse import urlparse, urlunparse

import httpx

from networking.dialer import TailnetPeerLookup, tailnet_dial_reserve_seconds

DEFAULT_INTERNAL_HTTP_TIMEOUT_SECONDS = 30.0
# A peer absent from the netmap needs it to catch up; one already known must not
# pay for that on every request.
DEFAULT_PEER_WAIT_SECONDS = 10.0

# The failures worth resolving a peer for, and the only ones it is safe to retry
# after: httpx raises these before it reads the request body, so a redial does
# not have to replay a stream it has already consumed.
_CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout)


class InternalHttpError(RuntimeError):
    """An internal hop could not be completed."""


class TailnetPeerAddressResolver(Protocol):
    def peer_address(self, host: str, *, timeout_seconds: float) -> str: ...


@dataclass(frozen=True, slots=True)
class TailnetHostPolicy:
    """Decides whether a destination belongs to the tailnet.

    The suffix is derived from the runtime's own identity rather than configured
    separately: a node already knows which tailnet it joined, and a second place
    to state it is a second place for it to be wrong.
    """

    dns_suffix: str = ""

    def covers(self, host: str) -> bool:
        suffix = self.dns_suffix.strip().rstrip(".").lower()
        if not suffix:
            return False
        candidate = host.strip().rstrip(".").lower()
        return candidate == suffix or candidate.endswith(f".{suffix}")


@dataclass(slots=True)
class TailnetPeerAddresses:
    """Resolves tailnet hosts to peer addresses, waiting out a stale netmap."""

    runtime: TailnetPeerLookup
    policy: TailnetHostPolicy
    wait_seconds: float = DEFAULT_PEER_WAIT_SECONDS
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def peer_address(self, host: str, *, timeout_seconds: float) -> str:
        """Wait out a stale netmap and return the peer's address.

        Called only after dialing the name has already failed, so the wait is
        recovery rather than a toll on every request.
        """
        if not self.policy.covers(host):
            return ""
        budget = min(self.wait_seconds, max(timeout_seconds, 0.001))
        reserve = tailnet_dial_reserve_seconds(budget)
        with self._lock:
            try:
                self.runtime.wait_for_peer(host, max(budget - reserve, 0.001))
                address = self.runtime.resolve_peer_host(host)
            except TimeoutError as exc:
                msg = f"tailnet peer {host} did not become reachable: {exc}"
                raise InternalHttpError(msg) from exc
        if not address:
            msg = f"tailnet peer {host} has no reachable address"
            raise InternalHttpError(msg)
        return address


@dataclass(slots=True)
class InternalHttpClient:
    """The single client for platform-internal HTTP.

    Construct one per process and share it: connections are pooled, so a peer
    that answers to its name is dialed without consulting the tailnet at all.

    The tailnet name is the route. Peer resolution is the recovery step, taken
    only when connecting by name fails, because resolving first costs a
    loopback hop and two `tailscale status` executions on every request and can
    drive the node's own control session down under ordinary load.
    """

    timeout_seconds: float = DEFAULT_INTERNAL_HTTP_TIMEOUT_SECONDS
    addresses: TailnetPeerAddressResolver | None = None
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
        return self._client

    def _peer_route(
        self, url: str, timeout_seconds: float
    ) -> tuple[str, dict[str, str], dict[str, str]] | None:
        """Where to redial `url` once its name did not connect, or None if it is not a peer."""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not host or self.addresses is None:
            return None
        address = self.addresses.peer_address(host, timeout_seconds=timeout_seconds)
        if not address:
            return None
        port = f":{parsed.port}" if parsed.port else ""
        literal = f"[{address}]" if ":" in address else address
        dialed = urlunparse(parsed._replace(netloc=f"{literal}{port}"))
        # `Host` keeps the name the caller signed and routed against; SNI keeps
        # the certificate valid for it. Only the connection target changes.
        return dialed, {"Host": f"{host}{port}"}, {"sni_hostname": host}

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | IO[bytes] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        timeout = timeout_seconds or self.timeout_seconds
        merged = dict(headers or {})
        try:
            return self._ensure_client().request(
                method,
                url,
                headers=merged,
                content=content,
                timeout=timeout,
            )
        except _CONNECT_FAILURES as exc:
            route = self._peer_route(url, timeout)
            if route is None:
                raise InternalHttpError(f"{method} {_safe_target(url)} failed: {exc}") from exc
            dialed, host_headers, extensions = route
            try:
                return self._ensure_client().request(
                    method,
                    dialed,
                    headers={**merged, **host_headers},
                    content=content,
                    timeout=timeout,
                    extensions=extensions,
                )
            except httpx.HTTPError as retry_exc:
                msg = f"{method} {_safe_target(url)} failed: {retry_exc}"
                raise InternalHttpError(msg) from retry_exc
        except httpx.HTTPError as exc:
            raise InternalHttpError(f"{method} {_safe_target(url)} failed: {exc}") from exc

    @contextmanager
    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | IO[bytes] | None = None,
        timeout_seconds: float | None = None,
    ) -> Iterator[httpx.Response]:
        timeout = timeout_seconds or self.timeout_seconds
        merged = dict(headers or {})
        client = self._ensure_client()
        opened = client.stream(method, url, headers=merged, content=content, timeout=timeout)
        try:
            response = opened.__enter__()
        except _CONNECT_FAILURES as exc:
            route = self._peer_route(url, timeout)
            if route is None:
                raise InternalHttpError(f"{method} {_safe_target(url)} failed: {exc}") from exc
            dialed, host_headers, extensions = route
            opened = client.stream(
                method,
                dialed,
                headers={**merged, **host_headers},
                content=content,
                timeout=timeout,
                extensions=extensions,
            )
            try:
                response = opened.__enter__()
            except httpx.HTTPError as retry_exc:
                msg = f"{method} {_safe_target(url)} failed: {retry_exc}"
                raise InternalHttpError(msg) from retry_exc
        except httpx.HTTPError as exc:
            raise InternalHttpError(f"{method} {_safe_target(url)} failed: {exc}") from exc
        try:
            yield response
        except httpx.HTTPError as exc:
            raise InternalHttpError(f"{method} {_safe_target(url)} failed: {exc}") from exc
        finally:
            opened.__exit__(None, None, None)

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            client.close()


def _safe_target(url: str) -> str:
    """A URL rendered for an error message, without credentials or query."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path}"


__all__ = [
    "DEFAULT_INTERNAL_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_PEER_WAIT_SECONDS",
    "InternalHttpClient",
    "InternalHttpError",
    "TailnetHostPolicy",
    "TailnetPeerAddressResolver",
    "TailnetPeerAddresses",
]
