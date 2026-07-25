from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from control.service import ControlPlaneService, StubKind, StubRecord
from shared.deployment_records import Deployment
from shared.errors import NotFoundError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from api.server.deployed_stubs import stub_is_public
from api.server.services import ApiServices

VERSIONED_HOST_PATTERN = re.compile(r"^(?P<name>.+)-v(?P<version>[1-9][0-9]*)$")
PORT_HOST_PATTERN = re.compile(r"^(?P<target>.+)-(?P<port>[1-9][0-9]{0,4})$")
PROXY_STUB_KINDS = {StubKind.Pod, StubKind.Sandbox}


class ApiServicesProvider(Protocol):
    def current(self) -> ApiServices: ...


class GeneratedInvokeHostRoutingMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        services_provider: ApiServicesProvider,
    ) -> None:
        self.app = app
        self.services_provider = services_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        services = self.services_provider.current()
        base_host = (urlparse(services.gateway_settings.public_http_url).hostname or "").lower()
        if not base_host:
            await self.app(scope, receive, send)
            return
        subdomain = self._subdomain(scope, base_host=base_host)
        if not subdomain:
            await self.app(scope, receive, send)
            return
        handler_path = _resolve_handler_path(
            services,
            subdomain,
            original_path=str(scope.get("path") or "/"),
        )
        if handler_path is None:
            await self.app(scope, receive, send)
            return
        rewritten = dict(scope)
        rewritten["path"] = handler_path
        rewritten["raw_path"] = handler_path.encode("utf-8")
        await self.app(rewritten, receive, send)

    @staticmethod
    def _subdomain(scope: Scope, *, base_host: str) -> str:
        host = _scope_host(scope).split(":", 1)[0].strip(".").lower()
        if not host or host == base_host:
            return ""
        suffix = f".{base_host}"
        if not host.endswith(suffix):
            return ""
        return host[: -len(suffix)]


@dataclass(frozen=True, slots=True)
class _HostTarget:
    stub: StubRecord
    port: int | None = None
    container_id: str | None = None
    deployment_name: str = ""
    deployment_version: int | None = None
    latest: bool = False
    stub_id_route: bool = False


def _resolve_handler_path(
    services: ApiServices,
    subdomain: str,
    *,
    original_path: str,
) -> str | None:
    control_plane = ControlPlaneService(services.context)
    target = _resolve_host_target(services, control_plane, subdomain)
    if target is None:
        return None
    prefix = _kind_path(target.stub.kind)
    if prefix is None:
        return None
    if target.stub.kind in PROXY_STUB_KINDS and target.port is None:
        return None
    route_id = target.container_id or target.stub.id
    if stub_is_public(services.apps, target.stub):
        base_path = f"/{prefix}/public/{route_id}"
    elif target.stub_id_route:
        base_path = f"/{prefix}/id/{route_id}"
    elif target.deployment_version is not None:
        base_path = f"/{prefix}/{target.deployment_name}/v{target.deployment_version}"
    elif target.latest:
        base_path = f"/{prefix}/{target.deployment_name}/latest"
    else:
        base_path = f"/{prefix}/id/{target.stub.id}"
    if target.port is not None:
        base_path = f"{base_path}/{target.port}"
    return _join_paths(base_path, original_path)


def _resolve_host_target(
    services: ApiServices,
    control_plane: ControlPlaneService,
    subdomain: str,
) -> _HostTarget | None:
    try:
        stub = control_plane.get_stub(subdomain)
    except NotFoundError:
        stub = None
    if stub is not None:
        return _HostTarget(stub=stub, stub_id_route=True)

    port_match = PORT_HOST_PATTERN.match(subdomain)
    if port_match is not None:
        maybe_port = int(port_match.group("port"))
        if maybe_port <= 65535:
            routed_name = port_match.group("target")
            try:
                container = services.containers.get(routed_name)
            except NotFoundError:
                container = None
            if container is not None and container.stub_id is not None:
                try:
                    stub = control_plane.get_stub(
                        container.stub_id,
                        workspace=container.workspace_id,
                    )
                except NotFoundError:
                    stub = None
                if (
                    stub is not None
                    and stub.kind is StubKind.Sandbox
                    and stub.workspace_id == container.workspace_id
                ):
                    return _HostTarget(
                        stub=stub,
                        port=maybe_port,
                        container_id=container.id,
                        stub_id_route=True,
                    )
            try:
                stub = control_plane.get_stub(routed_name)
            except NotFoundError:
                stub = None
            if stub is not None and stub.kind is StubKind.Pod:
                return _HostTarget(stub=stub, port=maybe_port, stub_id_route=True)
            target = _deployment_host_target_from_name(
                services,
                control_plane,
                routed_name,
                port=maybe_port,
            )
            if target is not None and target.stub.kind in PROXY_STUB_KINDS:
                return target

    return _deployment_host_target_from_name(services, control_plane, subdomain, port=None)


def _deployment_host_target_from_name(
    services: ApiServices,
    control_plane: ControlPlaneService,
    routed_name: str,
    *,
    port: int | None,
) -> _HostTarget | None:
    latest = False
    version: int | None = None
    name = routed_name
    if routed_name.endswith("-latest"):
        latest = True
        name = routed_name.removesuffix("-latest")
    else:
        match = VERSIONED_HOST_PATTERN.match(routed_name)
        if match is not None:
            name = match.group("name")
            version = int(match.group("version"))
    return _deployment_host_target(
        services,
        control_plane,
        name,
        version=version,
        latest=latest,
        port=port,
    )


def _deployment_host_target(
    services: ApiServices,
    control_plane: ControlPlaneService,
    name_or_route: str,
    *,
    version: int | None,
    latest: bool,
    port: int | None,
) -> _HostTarget | None:
    resources = services.deployment_resources.list(
        workspace=None,
        version=version,
        active=True,
    )
    resources.sort(key=lambda item: item.deployment.version, reverse=True)
    for resource in resources:
        deployment = resource.deployment
        if name_or_route not in _deployment_host_names(deployment, app_name=resource.app.name):
            continue
        return _HostTarget(
            stub=resource.stub,
            port=port,
            deployment_name=deployment.name,
            deployment_version=deployment.version if version is not None else None,
            latest=latest or version is None,
        )
    return None


def _deployment_host_names(deployment: Deployment, *, app_name: str = "") -> set[str]:
    names = {deployment.name, app_name}
    for key in ("subdomain", "deployment_subdomain"):
        value = deployment.spec.metadata.get(key)
        if isinstance(value, str) and value:
            names.add(value)
    return {name for name in names if name}


def _scope_host(scope: Scope) -> str:
    return Headers(scope=scope).get("host", "")


def _kind_path(kind: StubKind) -> str | None:
    if kind is StubKind.TaskQueue:
        return "api/v1/taskqueues"
    if kind is StubKind.Function:
        return "api/v1/functions"
    if kind is StubKind.Endpoint:
        return "api/v1/endpoints"
    if kind is StubKind.Asgi:
        return "api/v1/asgi"
    if kind in PROXY_STUB_KINDS:
        return kind.value
    return None


def _join_paths(base_path: str, original_path: str) -> str:
    if original_path in {"", "/"}:
        return base_path
    suffix = original_path if original_path.startswith("/") else f"/{original_path}"
    return f"{base_path.rstrip('/')}{suffix}"
