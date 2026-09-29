from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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
from shared.timestamps import utc_now

from agent.routes import AgentRoutes

LOGGER = logging.getLogger(__name__)
IP_FREEBIND = 15
RECONNECT_FIRST_DELAY_SECONDS = 0.2
"""Short, because a resumed machine's first dials fail until its DNS returns."""
RECONNECT_MAX_DELAY_SECONDS = 30.0
CERTIFICATE_RETRY_SECONDS = 5.0


class AgentTunnelFirewall(Protocol):
    def ensure(self) -> None: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class AgentTunnelService:
    state_dir: Path
    identity: AgentTunnelIdentity
    issue_certificate: Callable[[AgentCertificateRequest], AgentCertificateResponse]
    callback_firewall: AgentTunnelFirewall
    agent_token: str = field(repr=False)
    routes: AgentRoutes
    _loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _runner: asyncio.Task[None] | None = field(default=None, init=False)
    _route_runner: asyncio.Task[None] | None = field(default=None, init=False)
    _ready: Future[None] = field(default_factory=Future, init=False)
    _session: AgentTunnelClient | None = field(default=None, init=False)
    _retiring: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _streams: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _route_streams: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _servers: dict[str, asyncio.Server] = field(default_factory=dict, init=False)
    _failure: BaseException | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)
    _firewall_active: bool = field(default=False, init=False)
    _connected: threading.Event = field(default_factory=threading.Event, init=False)
    _redial: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    @property
    def connected(self) -> bool:
        session = self._session
        return self._connected.is_set() and session is not None and session.accepting

    def wait_connected(self, timeout_seconds: float) -> bool:
        """Wait up to `timeout_seconds` for a session; True once one is connected."""
        return self._connected.wait(timeout_seconds)

    def redial(self) -> None:
        """Replace the session now, as after a sleep that left its connection dead.

        The tunnel reads as disconnected from this call until a new session
        connects, so nothing opens a worker listener onto the old one.
        """
        self._connected.clear()
        self._loop.call_soon_threadsafe(self._redial.set)

    def start(
        self,
        *,
        callback_hosts: set[str],
        listen: bool = True,
    ) -> None:
        """Connect, then open the worker control listeners unless `listen` is false.

        Listeners open only once connected: a worker that finds no listener
        retries, while one accepted before the session exists is disconnected
        and gives up. Without `listen` they stay closed until
        `reconcile_listeners`, which keeps a reserve's worker from the control
        plane until a stream decides about it.
        """
        if self._thread is not None:
            raise RuntimeError("Agent tunnel service already started")
        try:
            self._firewall_active = True
            self.callback_firewall.ensure()
            self._thread = threading.Thread(
                target=self._run,
                args=(set(callback_hosts) if listen else None,),
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

    def hold_listeners(self) -> None:
        """Close every worker control listener, holding workers off the control plane."""
        self.check()
        asyncio.run_coroutine_threadsafe(self._reconcile_listeners(None), self._loop).result()

    def observe_route_revision(self, revision: int) -> None:
        self._loop.call_soon_threadsafe(self.routes.observe_revision, revision)

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

    def _run(self, callback_hosts: set[str] | None) -> None:
        asyncio.set_event_loop(self._loop)
        self._route_runner = self._loop.create_task(self.routes.run())
        self._route_runner.add_done_callback(self._finished)
        self._runner = self._loop.create_task(self._maintain(callback_hosts))
        self._runner.add_done_callback(self._finished)
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
            self._loop.close()

    def _finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = error
            if not self._ready.done():
                self._ready.set_exception(error)

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

    async def _maintain(self, callback_hosts: set[str] | None) -> None:
        credentials = TunnelCredentials(
            key_path=self.state_dir / "tunnel" / "agent.key",
            bundle_path=self.state_dir / "tunnel" / "certificate.json",
        )
        certificate = await self._certificate(credentials)
        retry_delay = RECONNECT_FIRST_DELAY_SECONDS
        session: AgentTunnelClient | None = None
        try:
            while True:
                session = None
                try:
                    if _renewal_due(certificate):
                        certificate = await self._certificate(credentials)
                    session = AgentTunnelClient(
                        certificate.tunnel_address,
                        credentials,
                        certificate.expires_at,
                        self.routes.targets.get,
                        self.routes.accept_update,
                        streams=self._streams,
                        route_streams=self._route_streams,
                    )
                    await session.start()
                    self._session = session
                    self._redial.clear()
                    self._connected.set()
                    self.routes.accept_update(None)
                    if not self._ready.done():
                        if callback_hosts is not None:
                            await self._reconcile_listeners(callback_hosts)
                        self._ready.set_result(None)
                    LOGGER.info("Agent tunnel connected connection_id=%s", session.connection_id)
                    retry_delay = RECONNECT_FIRST_DELAY_SECONDS
                    certificate = await self._serve(session, credentials, certificate)
                    self._retire(session)
                except AgentTunnelRevokedError:
                    if session is not None:
                        await session.close()
                    raise
                except (
                    ConnectionError,
                    OSError,
                    TimeoutError,
                    grpc.RpcError,
                    HttpApiError,
                    HttpTransportError,
                ) as exc:
                    if session is not None:
                        self._retire(session)
                    LOGGER.warning("Agent tunnel reconnecting: %s", type(exc).__name__)
                    if not self._ready.done():
                        raise
                    retry_delay = await self._back_off(retry_delay)
                finally:
                    self._connected.clear()
                    self._session = None
        finally:
            if session is not None:
                await session.close()
            await self._close_sessions()

    async def _back_off(self, delay: float) -> float:
        """Wait `delay` before dialing again, and return the next delay.

        A redial, as after a resume, ends the wait and starts the delays over.
        """
        try:
            async with asyncio.timeout(delay):
                await self._redial.wait()
        except TimeoutError:
            return min(RECONNECT_MAX_DELAY_SECONDS, delay * 2)
        self._redial.clear()
        return RECONNECT_FIRST_DELAY_SECONDS

    async def _serve(
        self,
        session: AgentTunnelClient,
        credentials: TunnelCredentials,
        certificate: AgentCertificateResponse,
    ) -> AgentCertificateResponse:
        """Serve until the session ends or a redial is asked for, renewing its certificate.

        Returns the certificate the next session connects with. A renewal that
        fails keeps the current session and is tried again later.
        """
        renew_in = _seconds_until_renewal(certificate)
        while True:
            ended = asyncio.ensure_future(session.wait_disconnected())
            redial = asyncio.ensure_future(self._redial.wait())
            done, _ = await asyncio.wait(
                {ended, redial}, timeout=max(renew_in, 1.0), return_when=asyncio.FIRST_COMPLETED
            )
            ended.cancel()
            redial.cancel()
            await asyncio.gather(ended, redial, return_exceptions=True)
            if ended in done:
                ended.result()
                return certificate
            if redial in done:
                self._redial.clear()
                LOGGER.info("Agent tunnel redialing connection_id=%s", session.connection_id)
                return certificate
            try:
                return await self._certificate(credentials)
            except AgentTunnelRevokedError:
                raise
            except (OSError, HttpApiError, HttpTransportError) as exc:
                LOGGER.warning("Agent certificate renewal failed: %s", type(exc).__name__)
                renew_in = CERTIFICATE_RETRY_SECONDS

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

    async def _reconcile_listeners(self, hosts: set[str] | None) -> None:
        wanted: set[str] = set() if hosts is None else {"127.0.0.1", *hosts}
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
        if self._route_runner is not None:
            self._route_runner.cancel()
            await asyncio.gather(self._route_runner, return_exceptions=True)
        await self._close_sessions()
        await asyncio.gather(*(server.wait_closed() for server in self._servers.values()))
        self._servers.clear()


def _seconds_until_renewal(certificate: AgentCertificateResponse) -> float:
    remaining = (certificate.expires_at - utc_now()).total_seconds()
    return remaining - TUNNEL_CERTIFICATE_RENEWAL_MARGIN_SECONDS


def _renewal_due(certificate: AgentCertificateResponse) -> bool:
    return _seconds_until_renewal(certificate) <= 0
