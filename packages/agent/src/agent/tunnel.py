from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import grpc
from networking.tunnel_agent import AgentTunnelClient, AgentTunnelRevokedError
from networking.tunnel_tls import TunnelCredentials, agent_certificate_request
from shared.agent_connections import AGENT_TUNNEL_CONTROL_PORT
from shared.http.agent_identity import (
    TUNNEL_CERTIFICATE_RENEWAL_MARGIN_SECONDS,
    AgentCertificateRequest,
    AgentCertificateResponse,
    AgentTunnelIdentity,
)
from shared.http.errors import HttpApiError, HttpTransportError
from shared.routing import BackendRouteState
from shared.timestamps import utc_now

LOGGER = logging.getLogger(__name__)
IP_FREEBIND = 15


class AgentTunnelFirewall(Protocol):
    def ensure(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AgentTunnelRoute:
    route_id: str
    local_target: str
    state: BackendRouteState


@dataclass(slots=True)
class AgentTunnelService:
    state_dir: Path
    identity: AgentTunnelIdentity
    issue_certificate: Callable[[AgentCertificateRequest], AgentCertificateResponse]
    callback_firewall: AgentTunnelFirewall
    agent_token: str = field(repr=False)
    _loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _runner: asyncio.Task[None] | None = field(default=None, init=False)
    _ready: Future[None] = field(default_factory=Future, init=False)
    _session: AgentTunnelClient | None = field(default=None, init=False)
    _retiring: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _streams: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _servers: dict[str, asyncio.Server] = field(default_factory=dict, init=False)
    _routes: dict[str, tuple[str, int]] = field(default_factory=dict, init=False)
    _failure: BaseException | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)
    _firewall_active: bool = field(default=False, init=False)

    @property
    def connected(self) -> bool:
        return self._session is not None and self._session.accepting

    def start(self, *, callback_hosts: set[str], routes: Sequence[AgentTunnelRoute]) -> None:
        if self._thread is not None:
            raise RuntimeError("Agent tunnel service already started")
        try:
            self._firewall_active = True
            self.callback_firewall.ensure()
            self._thread = threading.Thread(
                target=self._run,
                args=(set(callback_hosts), tuple(routes)),
                name="agent-tunnel",
                daemon=True,
            )
            self._thread.start()
            self._ready.result()
        except BaseException:
            self.close()
            raise

    def check(self) -> None:
        if self._failure is not None:
            if isinstance(self._failure, AgentTunnelRevokedError):
                raise self._failure
            raise RuntimeError("Agent tunnel owner exited") from self._failure
        if not self.connected:
            raise ConnectionError("Agent tunnel is reconnecting")

    def reconcile_listeners(self, hosts: set[str]) -> None:
        self.check()
        asyncio.run_coroutine_threadsafe(self._reconcile_listeners(hosts), self._loop).result()

    def reconcile_routes(self, routes: Sequence[AgentTunnelRoute]) -> set[str]:
        self.check()
        return asyncio.run_coroutine_threadsafe(self._reconcile_routes(routes), self._loop).result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._thread is None:
                self._loop.close()
                return
            asyncio.run_coroutine_threadsafe(self._close(), self._loop).result()
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
        finally:
            if self._firewall_active:
                self.callback_firewall.close()
                self._firewall_active = False

    def _run(self, callback_hosts: set[str], routes: Sequence[AgentTunnelRoute]) -> None:
        asyncio.set_event_loop(self._loop)
        self._runner = self._loop.create_task(self._maintain(callback_hosts, routes))
        self._runner.add_done_callback(self._finished)
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
            self._loop.close()

    def _finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            self._failure = task.exception()
            if self._failure is not None and not self._ready.done():
                self._ready.set_exception(self._failure)

    async def _certificate(self, credentials: TunnelCredentials) -> AgentCertificateResponse:
        request = AgentCertificateRequest(
            agent_token=self.agent_token,
            csr_pem=await asyncio.to_thread(agent_certificate_request, credentials.key_path),
        )
        try:
            certificate = await asyncio.to_thread(self.issue_certificate, request)
        except HttpApiError as exc:
            if exc.status_code in {401, 403, 409}:
                raise AgentTunnelRevokedError(
                    "Agent tunnel credential is no longer current"
                ) from exc
            raise
        if certificate.identity != self.identity:
            raise AgentTunnelRevokedError("Certificate names a different agent enrollment")
        await asyncio.to_thread(
            credentials.install,
            certificate_pem=certificate.certificate_pem,
            trust_bundle_pem=certificate.trust_bundle_pem,
        )
        return certificate

    async def _maintain(self, callback_hosts: set[str], routes: Sequence[AgentTunnelRoute]) -> None:
        credentials = TunnelCredentials(
            key_path=self.state_dir / "tunnel" / "agent.key",
            bundle_path=self.state_dir / "tunnel" / "certificate.json",
        )
        certificate = await self._certificate(credentials)
        await self._reconcile_listeners(callback_hosts)
        await self._reconcile_routes(routes)
        retry_delay = 1.0
        session: AgentTunnelClient | None = None
        try:
            while True:
                session = AgentTunnelClient(
                    certificate.tunnel_address,
                    credentials,
                    certificate.expires_at,
                    self._routes.get,
                    streams=self._streams,
                )
                try:
                    await session.start()
                    self._session = session
                    if not self._ready.done():
                        self._ready.set_result(None)
                    LOGGER.info("Agent tunnel connected connection_id=%s", session.connection_id)
                    retry_delay = 1.0
                    while session.accepting:
                        remaining = (
                            certificate.expires_at - utc_now()
                        ).total_seconds() - TUNNEL_CERTIFICATE_RENEWAL_MARGIN_SECONDS
                        try:
                            async with asyncio.timeout(max(1, remaining)):
                                await session.wait_disconnected()
                            break
                        except TimeoutError:
                            try:
                                certificate = await self._certificate(credentials)
                            except AgentTunnelRevokedError:
                                raise
                            except (OSError, HttpApiError, HttpTransportError) as exc:
                                LOGGER.warning(
                                    "Agent certificate renewal failed: %s", type(exc).__name__
                                )
                                await asyncio.sleep(5)
                                continue
                            break
                    self._retire(session)
                except AgentTunnelRevokedError:
                    await session.close()
                    raise
                except (ConnectionError, OSError, TimeoutError, grpc.RpcError, HttpApiError) as exc:
                    self._retire(session)
                    LOGGER.warning("Agent tunnel reconnecting: %s", type(exc).__name__)
                    if not self._ready.done():
                        raise
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(30, retry_delay * 2)
                finally:
                    self._session = None
                if certificate.expires_at <= utc_now():
                    certificate = await self._certificate(credentials)
        finally:
            if session is not None:
                await session.close()
            await self._close_sessions()

    def _retire(self, session: AgentTunnelClient) -> None:
        session.accepting = False
        task = asyncio.create_task(session.retire())
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    def _control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._session is None:
            writer.close()
        else:
            self._session.forward_control(reader, writer)

    async def _reconcile_listeners(self, hosts: set[str]) -> None:
        wanted = {"127.0.0.1", *hosts}
        for host in wanted - self._servers.keys():
            address = ipaddress.ip_address(host)
            if address.version != 4 or address.is_unspecified or address.is_multicast:
                raise ValueError("Agent control listener requires an owned IPv4 address")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                # The callback listener precedes the worker's bridge creation.
                listener.setsockopt(socket.IPPROTO_IP, IP_FREEBIND, 1)
                listener.bind((host, AGENT_TUNNEL_CONTROL_PORT))
                listener.listen(128)
                listener.setblocking(False)
                self._servers[host] = await asyncio.start_server(self._control, sock=listener)
            except BaseException:
                listener.close()
                raise
        for host in self._servers.keys() - wanted:
            server = self._servers.pop(host)
            server.close()
            await server.wait_closed()

    async def _reconcile_routes(self, routes: Sequence[AgentTunnelRoute]) -> set[str]:
        registered: dict[str, tuple[str, int]] = {}
        newly_ready: set[str] = set()
        for route in routes:
            if not route.route_id or not route.local_target:
                continue
            target = urlsplit(f"//{route.local_target}")
            if (
                target.username is not None
                or target.password is not None
                or target.path
                or target.query
                or target.fragment
                or not target.hostname
                or target.port is None
            ):
                raise ValueError("Agent route requires a registered host and port")
            registered[route.route_id] = (target.hostname, target.port)
            if route.state is BackendRouteState.Ready:
                continue
            try:
                async with asyncio.timeout(0.25):
                    _, writer = await asyncio.open_connection(target.hostname, target.port)
                    writer.close()
                    await writer.wait_closed()
            except OSError:
                continue
            newly_ready.add(route.route_id)
        self._routes.clear()
        self._routes.update(registered)
        return newly_ready

    async def _close_sessions(self) -> None:
        if self._session is not None:
            await self._session.close()
        for task in tuple(self._streams):
            task.cancel()
        await asyncio.gather(*self._streams, return_exceptions=True)
        await asyncio.gather(*self._retiring, return_exceptions=True)

    async def _close(self) -> None:
        for server in self._servers.values():
            server.close()
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        await self._close_sessions()
        await asyncio.gather(*(server.wait_closed() for server in self._servers.values()))
        self._servers.clear()
