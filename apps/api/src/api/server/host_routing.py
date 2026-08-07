from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from control.service import ControlPlaneService, StubKind, StubRecord
from shared.deployment_subdomains import parse_deployment_host
from shared.errors import NotFoundError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from api.server.deployed_stubs import stub_is_public
from api.server.services import ApiServices

PORT_HOST_PATTERN = re.compile(r"^(?P<target>.+)-(?P<port>[1-9][0-9]{0,4})$")
PROXY_STUB_KINDS = {StubKind.Pod, StubKind.Sandbox}


class ApiServicesProvider(Protocol):
    def current(self) -> ApiServices: ...


class GeneratedInvokeHostRoutingMiddleware:
    """Route a public request by the hostname it arrived on.

    The one place a hostname becomes a resource. A request here carries no token, so
    the host is the only routing key there is, and the workspace comes from whatever
    row the host resolves to rather than from the caller.
    """

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
        base_host = services.gateway_settings.public_base_domain
        if not base_host:
            await self.app(scope, receive, send)
            return
        label = self._host_label(scope, base_host=base_host)
        if not label:
            await self.app(scope, receive, send)
            return
        handler_path = _resolve_handler_path(
            services,
            label,
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
    def _host_label(scope: Scope, *, base_host: str) -> str:
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
    stub_id_route: bool = False


def _resolve_handler_path(
    services: ApiServices,
    label: str,
    *,
    original_path: str,
) -> str | None:
    control_plane = ControlPlaneService(services.context)
    target = _resolve_host_target(services, control_plane, label)
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
    else:
        base_path = f"/{prefix}/{target.deployment_name}/latest"
    if target.port is not None:
        base_path = f"{base_path}/{target.port}"
    return _join_paths(base_path, original_path)


def _resolve_host_target(
    services: ApiServices,
    control_plane: ControlPlaneService,
    label: str,
) -> _HostTarget | None:
    try:
        stub = control_plane.get_stub(label)
    except NotFoundError:
        stub = None
    if stub is not None:
        return _HostTarget(stub=stub, stub_id_route=True)

    port_target = _port_host_target(services, control_plane, label)
    if port_target is not None:
        return port_target

    return _deployment_host_target(services, label)


def _port_host_target(
    services: ApiServices,
    control_plane: ControlPlaneService,
    label: str,
) -> _HostTarget | None:
    """Resolve `<container-or-stub>-<port>`, whose ids are already globally unique."""

    port_match = PORT_HOST_PATTERN.match(label)
    if port_match is None:
        return None
    port = int(port_match.group("port"))
    if port > 65535:
        return None
    routed_name = port_match.group("target")
    try:
        container = services.containers.get(routed_name)
    except NotFoundError:
        container = None
    if container is not None and container.stub_id is not None:
        try:
            stub = control_plane.get_stub(container.stub_id, workspace=container.workspace_id)
        except NotFoundError:
            stub = None
        if (
            stub is not None
            and stub.kind is StubKind.Sandbox
            and stub.workspace_id == container.workspace_id
        ):
            return _HostTarget(
                stub=stub,
                port=port,
                container_id=container.id,
                stub_id_route=True,
            )
    try:
        stub = control_plane.get_stub(routed_name)
    except NotFoundError:
        stub = None
    if stub is not None and stub.kind is StubKind.Pod:
        return _HostTarget(stub=stub, port=port, stub_id_route=True)
    return None


def _deployment_host_target(services: ApiServices, label: str) -> _HostTarget | None:
    parsed = parse_deployment_host(label)
    if parsed is None:
        return None
    resource = services.deployment_resources.get_by_subdomain(
        parsed.subdomain,
        version=parsed.version,
    )
    if resource is None:
        return None
    return _HostTarget(
        stub=resource.stub,
        deployment_name=resource.deployment.name,
        deployment_version=parsed.version,
    )


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
