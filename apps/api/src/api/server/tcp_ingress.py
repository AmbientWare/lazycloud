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
from coordination.redis_client import AsyncRedisClient, redis_text
from execution.pods.config import PodStubConfig
from execution.pods.planning import PodProxyProtocol
from execution.pods.proxy import PodProxySession
from execution.pods.service import PodControlService
from pydantic import Field
from shared.contracts import ContractModel
from shared.errors import NotFoundError
from sqlalchemy.orm import Session

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
    async def resolve(self, sni: str) -> TcpIngressRoute: ...


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
    redis: AsyncRedisClient
    external_host: str
    cache_ttl_seconds: int = 300
    control_plane: ControlPlaneService = field(init=False)

    def __post_init__(self) -> None:
        self.external_host = self.external_host.strip(".").lower()
        self.control_plane = ControlPlaneService(self.services.context)

    async def resolve(self, sni: str) -> TcpIngressRoute:
        normalized = sni.strip(".").lower()
        cache_key = self._cache_key(normalized)
        cached = await self.redis.get(cache_key)
        if cached is not None:
            try:
                route = TcpIngressRoute.model_validate_json(redis_text(cached))
                return await self._validated_route(route)
            except (KeyError, ValueError, TcpIngressRouteNotFound):
                await self.redis.delete(cache_key)
        stub_id, port = self._parse_sni(normalized)
        route = await self.services.require_async_io().database.run_transaction(
            lambda session: self._route_for_stub_in_session(
                session,
                normalized,
                stub_id,
                port,
            )
        )
        await self.redis.set(
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

    def _route_for_stub_in_session(
        self,
        session: Session,
        sni: str,
        stub_id: str,
        port: int,
    ) -> TcpIngressRoute:
        try:
            stub = self.control_plane.get_stub_in_session(session, stub_id)
        except NotFoundError as exc:
            raise TcpIngressRouteNotFound("TCP workload was not found") from exc
        self._validate_stub(stub, port)
        resources = [
            resource
            for resource in self.services.deployment_resources.list_in_session(
                session,
                workspace=stub.workspace_id,
                active=True,
            )
            if resource.stub.id == stub.id
        ]
        if not resources:
            raise TcpIngressRouteNotFound("TCP workload has no active deployment")
        resource = max(resources, key=lambda item: item.deployment.version)
        workspace = self.services.context.workspace(session, stub.workspace_id)
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

    async def _validated_route(self, route: TcpIngressRoute) -> TcpIngressRoute:
        if route.sni != tcp_ingress_hostname(route.stub_id, route.port, self.external_host):
            raise TcpIngressRouteNotFound("cached TCP route does not match SNI")
        return await self.services.require_async_io().database.run_transaction(
            lambda session: self._validated_route_in_session(session, route)
        )

    def _validated_route_in_session(
        self,
        session: Session,
        route: TcpIngressRoute,
    ) -> TcpIngressRoute:
        try:
            stub = self.control_plane.get_stub_in_session(session, route.stub_id)
        except NotFoundError as exc:
            raise TcpIngressRouteNotFound("cached TCP workload was deleted") from exc
        self._validate_stub(stub, route.port)
        active = self.services.deployment_resources.list_in_session(
            session,
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
    def __init__(
        self,
        certificate_file: Path,
        key_file: Path,
        context: ssl.SSLContext,
        signature: tuple[int, int, int, int],
    ) -> None:
        self.certificate_file = certificate_file
        self.key_file = key_file
        self._lock = threading.Lock()
        self._server_names: weakref.WeakKeyDictionary[ssl.SSLObject | ssl.SSLSocket, str] = (
            weakref.WeakKeyDictionary()
        )
        self._signature = signature
        self._context = context

        def select_context(
            ssl_object: ssl.SSLObject | ssl.SSLSocket,
            server_name: str | None,
            _: object,
        ) -> None:
            self._select_context(ssl_object, server_name)

        self._context.set_servername_callback(select_context)

    @classmethod
    async def create(cls, certificate_file: Path, key_file: Path) -> ReloadingTlsContext:
        context, signature = await asyncio.to_thread(
            cls._load_context,
            certificate_file,
            key_file,
        )
        return cls(certificate_file, key_file, context, signature)

    @property
    def server_context(self) -> ssl.SSLContext:
        return self._context

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
            ssl_object.context = self._context
            self._server_names[ssl_object] = (server_name or "").lower()

    async def reload_if_changed(self) -> None:
        try:
            signature = await asyncio.to_thread(
                _file_signature,
                self.certificate_file,
                self.key_file,
            )
        except OSError:
            logger.exception("TCP ingress certificate metadata read failed")
            return
        if signature == self._signature:
            return
        try:
            context, loaded_signature = await asyncio.to_thread(
                self._load_context,
                self.certificate_file,
                self.key_file,
            )
        except (OSError, ssl.SSLError):
            logger.exception("TCP ingress certificate reload failed; retaining last known pair")
            return
        with self._lock:
            self._context = context
            self._signature = loaded_signature
        logger.info("TCP ingress certificate reloaded")

    @staticmethod
    def _load_context(
        certificate_file: Path,
        key_file: Path,
    ) -> tuple[ssl.SSLContext, tuple[int, int, int, int]]:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate_file, key_file)
        return context, _file_signature(certificate_file, key_file)


def _file_signature(certificate_file: Path, key_file: Path) -> tuple[int, int, int, int]:
    certificate = certificate_file.stat()
    key = key_file.stat()
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
    tls: ReloadingTlsContext
    host: str
    port: int
    max_connections: int = 1024
    tls_handshake_timeout_seconds: float = 10.0
    _server: asyncio.Server | None = field(default=None, init=False)
    _certificate_reload_task: asyncio.Task[None] | None = field(default=None, init=False)
    _active_connections: int = field(default=0, init=False)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self.host,
            port=self.port,
            ssl=self.tls.server_context,
            ssl_handshake_timeout=self.tls_handshake_timeout_seconds,
        )
        self._certificate_reload_task = asyncio.create_task(self._reload_certificates())
        logger.info("TCP ingress listening on %s:%s", self.host, self.port)

    async def close(self) -> None:
        failures: list[BaseException] = []
        reload_task = self._certificate_reload_task
        self._certificate_reload_task = None
        if reload_task is not None:
            reload_task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await reload_task
            except BaseException as exc:
                failures.append(exc)
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
                await server.wait_closed()
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise BaseExceptionGroup("TCP ingress shutdown was incomplete", failures)

    async def _reload_certificates(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.tls.reload_if_changed()

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
            sni = self.tls.server_name(ssl_object)
            if not sni:
                raise TcpIngressRouteNotFound("TLS SNI is required")
            route = await self.route_resolver.resolve(sni)
            session = await self.pod_service.prepare_pod_proxy(
                stub_id=route.stub_id,
                port=route.port,
                path="",
                query_params={},
                protocol=PodProxyProtocol.Tcp,
            )
            backend = await self.pod_service.open_pod_proxy_socket(session)
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
                await self.pod_service.finish_pod_proxy(session)
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


async def tcp_ingress_server_from_settings(
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
        redis=services.require_async_io().redis,
        external_host=settings.external_host,
        cache_ttl_seconds=settings.route_cache_ttl_seconds,
    )
    return TcpIngressServer(
        route_resolver=resolver,
        pod_service=pod_service,
        tls=await ReloadingTlsContext.create(certificate_file, key_file),
        host=settings.host,
        port=settings.port,
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
