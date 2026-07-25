from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import threading
import weakref
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from control.service import ControlPlaneService, StubKind, StubRecord
from coordination.redis_client import RedisClient, redis_text
from execution.pods.config import PodStubConfig
from execution.pods.planning import PodProxyProtocol
from execution.pods.proxy import PodProxySession
from execution.pods.service import PodControlService
from pydantic import Field
from shared.contracts import ContractModel
from shared.errors import NotFoundError

from api.server.services import ApiServices
from api.settings import TcpIngressSettings

logger = logging.getLogger(__name__)


class TcpIngressError(RuntimeError):
    pass


class TcpIngressRouteNotFound(TcpIngressError):
    pass


class TcpIngressRoute(ContractModel):
    sni: str
    workspace_id: str
    workspace_name: str
    app_id: str
    app_name: str
    workload_name: str
    deployment_id: str
    deployment_version: int = Field(ge=1)
    stub_id: str
    port: int = Field(ge=1, le=65535)


class TcpIngressRouteResolver(Protocol):
    def resolve(self, sni: str) -> TcpIngressRoute: ...


def tcp_ingress_hostname(stub_id: str, port: int, external_host: str) -> str:
    normalized_stub = stub_id.strip().lower()
    normalized_host = external_host.strip(".").lower()
    if not normalized_stub or not normalized_host:
        raise ValueError("stub id and TCP ingress external host are required")
    if not 1 <= port <= 65535:
        raise ValueError("TCP ingress port must be between 1 and 65535")
    return f"{normalized_stub}-{port}.{normalized_host}"


@dataclass(slots=True)
class RedisTcpIngressRouteResolver:
    services: ApiServices
    redis: RedisClient
    external_host: str
    cache_ttl_seconds: int = 300
    control_plane: ControlPlaneService = field(init=False)

    def __post_init__(self) -> None:
        self.external_host = self.external_host.strip(".").lower()
        self.control_plane = ControlPlaneService(self.services.context)

    def resolve(self, sni: str) -> TcpIngressRoute:
        normalized = sni.strip(".").lower()
        cache_key = self._cache_key(normalized)
        cached = self.redis.get(cache_key)
        if cached is not None:
            try:
                route = TcpIngressRoute.model_validate_json(redis_text(cached))
                return self._validated_route(route)
            except (KeyError, ValueError, TcpIngressRouteNotFound):
                self.redis.delete(cache_key)
        stub_id, port = self._parse_sni(normalized)
        route = self._route_for_stub(normalized, stub_id, port)
        self.redis.set(
            cache_key,
            route.model_dump_json(),
            ex=max(self.cache_ttl_seconds, 1),
        )
        return route

    def _parse_sni(self, sni: str) -> tuple[str, int]:
        suffix = f".{self.external_host}"
        if not self.external_host or not sni.endswith(suffix):
            raise TcpIngressRouteNotFound("SNI is outside the configured TCP ingress domain")
        target = sni[: -len(suffix)]
        stub_id, separator, raw_port = target.rpartition("-")
        if not separator or not raw_port.isdigit():
            raise TcpIngressRouteNotFound("SNI must identify a workload stub and port")
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise TcpIngressRouteNotFound("SNI port is outside the valid TCP range")
        return stub_id, port

    def _route_for_stub(self, sni: str, stub_id: str, port: int) -> TcpIngressRoute:
        try:
            stub = self.control_plane.get_stub(stub_id)
        except NotFoundError as exc:
            raise TcpIngressRouteNotFound("TCP workload was not found") from exc
        self._validate_stub(stub, port)
        resources = [
            resource
            for resource in self.services.deployment_resources.list(
                workspace=stub.workspace_id,
                active=True,
            )
            if resource.stub.id == stub.id
        ]
        if not resources:
            raise TcpIngressRouteNotFound("TCP workload has no active deployment")
        resource = max(resources, key=lambda item: item.deployment.version)
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        return TcpIngressRoute(
            sni=sni,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            app_id=resource.app.id,
            app_name=resource.app.name,
            workload_name=resource.deployment.name,
            deployment_id=resource.deployment.id,
            deployment_version=resource.deployment.version,
            stub_id=stub.id,
            port=port,
        )

    def _validated_route(self, route: TcpIngressRoute) -> TcpIngressRoute:
        if route.sni != tcp_ingress_hostname(route.stub_id, route.port, self.external_host):
            raise TcpIngressRouteNotFound("cached TCP route does not match SNI")
        try:
            stub = self.control_plane.get_stub(route.stub_id)
        except NotFoundError as exc:
            raise TcpIngressRouteNotFound("cached TCP workload was deleted") from exc
        self._validate_stub(stub, route.port)
        active = self.services.deployment_resources.list(
            workspace=route.workspace_id,
            app=route.app_id,
            name=route.workload_name,
            version=route.deployment_version,
            active=True,
        )
        if not any(resource.stub.id == route.stub_id for resource in active):
            raise TcpIngressRouteNotFound("cached TCP deployment is no longer active")
        return route

    def _validate_stub(self, stub: StubRecord, port: int) -> None:
        if stub.kind is not StubKind.Pod:
            raise TcpIngressRouteNotFound("TCP ingress only supports Pod workloads")
        config = PodStubConfig.model_validate(stub.config, from_attributes=True)
        if port not in config.exposed_ports:
            raise TcpIngressRouteNotFound("requested TCP port is not exposed by the workload")
        if not stub.config.tcp:
            raise TcpIngressRouteNotFound("workload did not enable raw TCP ingress")
        if not stub.public:
            raise TcpIngressRouteNotFound("raw TCP ingress requires a public workload")

    def _cache_key(self, sni: str) -> str:
        return self.redis.key("tcp-ingress", "sni", sni)


class ReloadingTlsContext:
    def __init__(self, certificate_file: Path, key_file: Path) -> None:
        self.certificate_file = certificate_file
        self.key_file = key_file
        self._lock = threading.Lock()
        self._server_names: weakref.WeakKeyDictionary[ssl.SSLObject | ssl.SSLSocket, str] = (
            weakref.WeakKeyDictionary()
        )
        self._signature: tuple[int, int, int, int] | None = None
        self._context = self._load_context()
        self.server_context = self._context

        def select_context(
            ssl_object: ssl.SSLObject | ssl.SSLSocket,
            server_name: str | None,
            _: object,
        ) -> None:
            self._select_context(ssl_object, server_name)

        self.server_context.set_servername_callback(select_context)

    def server_name(self, ssl_object: ssl.SSLObject | ssl.SSLSocket | None) -> str:
        if ssl_object is None:
            return ""
        with self._lock:
            return self._server_names.pop(ssl_object, "")

    def _select_context(
        self,
        ssl_object: ssl.SSLObject | ssl.SSLSocket,
        server_name: str | None,
    ) -> None:
        with self._lock:
            self._reload_if_changed()
            ssl_object.context = self._context
            self._server_names[ssl_object] = (server_name or "").lower()

    def _reload_if_changed(self) -> None:
        signature = self._file_signature()
        if signature == self._signature:
            return
        try:
            context = self._build_context()
        except (OSError, ssl.SSLError):
            logger.exception("TCP ingress certificate reload failed; retaining last known pair")
            return
        self._context = context
        self._signature = signature
        logger.info("TCP ingress certificate reloaded")

    def _load_context(self) -> ssl.SSLContext:
        context = self._build_context()
        self._signature = self._file_signature()
        return context

    def _build_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.certificate_file, self.key_file)
        return context

    def _file_signature(self) -> tuple[int, int, int, int]:
        certificate = self.certificate_file.stat()
        key = self.key_file.stat()
        return (certificate.st_mtime_ns, certificate.st_size, key.st_mtime_ns, key.st_size)


class TlsStreamWriter(Protocol):
    def get_extra_info(
        self,
        name: Literal["ssl_object"],
        default: None = None,
    ) -> ssl.SSLObject | ssl.SSLSocket | None: ...

    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


@dataclass(slots=True)
class TcpIngressServer:
    route_resolver: TcpIngressRouteResolver
    pod_service: PodControlService
    host: str
    port: int
    certificate_file: Path
    key_file: Path
    max_connections: int = 1024
    tls_handshake_timeout_seconds: float = 10.0
    _server: asyncio.Server | None = field(default=None, init=False)
    _tls: ReloadingTlsContext = field(init=False)
    _active_connections: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._tls = ReloadingTlsContext(self.certificate_file, self.key_file)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self.host,
            port=self.port,
            ssl=self._tls.server_context,
            ssl_handshake_timeout=self.tls_handshake_timeout_seconds,
        )
        logger.info("TCP ingress listening on %s:%s", self.host, self.port)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: TlsStreamWriter,
    ) -> None:
        if self._active_connections >= self.max_connections:
            await _close_writer(writer)
            return
        self._active_connections += 1
        session: PodProxySession | None = None
        backend: socket.socket | None = None
        try:
            ssl_object: ssl.SSLObject | ssl.SSLSocket | None = writer.get_extra_info("ssl_object")
            sni = self._tls.server_name(ssl_object)
            if not sni:
                raise TcpIngressRouteNotFound("TLS SNI is required")
            route = await asyncio.to_thread(self.route_resolver.resolve, sni)
            session = await asyncio.to_thread(
                self.pod_service.prepare_pod_proxy,
                stub_id=route.stub_id,
                port=route.port,
                path="",
                query_params={},
                protocol=PodProxyProtocol.Tcp,
            )
            backend = await asyncio.to_thread(self.pod_service.open_pod_proxy_socket, session)
            backend.setblocking(False)
            await _proxy_bidirectional(reader, writer, backend)
        except TcpIngressError as exc:
            logger.warning("TCP ingress connection rejected: %s", exc)
        except (OSError, RuntimeError, ValueError):
            logger.exception("TCP ingress connection failed")
        finally:
            if backend is not None:
                backend.close()
            if session is not None:
                await asyncio.to_thread(self.pod_service.finish_pod_proxy, session)
            await _close_writer(writer)
            self._active_connections -= 1


async def _proxy_bidirectional(
    reader: asyncio.StreamReader,
    writer: TlsStreamWriter,
    backend: socket.socket,
) -> None:
    loop = asyncio.get_running_loop()

    async def client_to_backend() -> None:
        while data := await reader.read(64 * 1024):
            await loop.sock_sendall(backend, data)
        try:
            backend.shutdown(socket.SHUT_WR)
        except OSError:
            return

    async def backend_to_client() -> None:
        while data := await loop.sock_recv(backend, 64 * 1024):
            writer.write(data)
            await writer.drain()

    to_backend = asyncio.create_task(client_to_backend())
    to_client = asyncio.create_task(backend_to_client())
    try:
        done, _ = await asyncio.wait(
            {to_backend, to_client},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if to_client in done:
            await to_client
            to_backend.cancel()
        else:
            await to_backend
            await to_client
    finally:
        for task in (to_backend, to_client):
            if not task.done():
                task.cancel()
        for task in (to_backend, to_client):
            with suppress(asyncio.CancelledError):
                await task


async def _close_writer(writer: TlsStreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError, ssl.SSLError):
        return


def tcp_ingress_server_from_settings(
    services: ApiServices,
    pod_service: PodControlService,
    settings: TcpIngressSettings,
) -> TcpIngressServer | None:
    if not settings.enabled:
        return None
    certificate_file = settings.certificate_file
    key_file = settings.key_file
    if certificate_file is None or key_file is None:
        raise ValueError("TCP ingress certificate and key files are required")
    resolver = RedisTcpIngressRouteResolver(
        services=services,
        redis=services.redis(),
        external_host=settings.external_host,
        cache_ttl_seconds=settings.route_cache_ttl_seconds,
    )
    return TcpIngressServer(
        route_resolver=resolver,
        pod_service=pod_service,
        host=settings.host,
        port=settings.port,
        certificate_file=certificate_file,
        key_file=key_file,
        max_connections=settings.max_connections,
        tls_handshake_timeout_seconds=settings.tls_handshake_timeout_seconds,
    )


__all__ = [
    "RedisTcpIngressRouteResolver",
    "ReloadingTlsContext",
    "TcpIngressError",
    "TcpIngressRoute",
    "TcpIngressRouteNotFound",
    "TcpIngressServer",
    "tcp_ingress_hostname",
    "tcp_ingress_server_from_settings",
]
