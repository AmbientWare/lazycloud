from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address

from pydantic import Field, JsonValue, SecretStr, computed_field, field_validator
from shared.contracts import ContractModel

from networking.resolve import EndpointAddress, resolve_endpoint

BACKEND_ROUTE_DIAL_HOST = "backend.route"
BACKEND_ROUTE_ID_METADATA_KEY = "backend_route_id"
BACKEND_ROUTE_PREFACE = "BACKEND-ROUTE/2 "
# A worker has no tailnet client of its own and must not be given the
# tailscaled socket, which grants tailnet control rather than lookup. It asks
# the agent on the loopback it already shares, presenting the worker token the
# agent itself issued, so no new secret is introduced.
TAILNET_RESOLVE_PREFACE = "TAILNET-RESOLVE/1 "
BACKEND_ROUTE_CREDENTIAL_CONTEXT = b"backend-route-preface/v1\x00"


class GatewayProtocol(StrEnum):
    Http = "http"
    Https = "https"
    Tcp = "tcp"


@dataclass(frozen=True, slots=True)
class BackendRouteAuthenticator:
    secret: SecretStr

    def __post_init__(self) -> None:
        if len(self.secret.get_secret_value().encode()) < 32:
            raise ValueError("backend route authentication key must be at least 32 bytes")

    def credential(self, route_id: str) -> str:
        normalized_route_id = _normalized_route_id(route_id)
        digest = hmac.new(
            self.secret.get_secret_value().encode(),
            BACKEND_ROUTE_CREDENTIAL_CONTEXT + normalized_route_id.encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def verify(self, route_id: str, credential: str) -> bool:
        if not credential:
            return False
        return hmac.compare_digest(self.credential(route_id), credential)


class RoutePrewarmReason(StrEnum):
    PublicRoute = "public-route"
    KeepWarm = "keep-warm"
    Manual = "manual"


class BackendDialTarget(ContractModel):
    scheme: GatewayProtocol = GatewayProtocol.Http
    host: str
    port: int | None = None
    path: str = "/"

    @field_validator("port")
    @classmethod
    def port_must_be_valid(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 65535:
            msg = "port must be between 1 and 65535"
            raise ValueError(msg)
        return value

    @computed_field
    @property
    def url(self) -> str:
        port = f":{self.port}" if self.port is not None else ""
        path = self.path if self.path.startswith("/") else f"/{self.path}"
        return f"{self.scheme.value}://{self.host}{port}{path}"


class BackendDialPlan(ContractModel):
    target: BackendDialTarget
    via_tailnet_peer: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class SubdomainRoute(ContractModel):
    host: str
    root_domain: str
    subdomain: str
    app: str
    workspace: str | None = None


class TailnetPeer(ContractModel):
    name: str
    hostname: str
    ipv4: IPv4Address | None = None
    tags: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)


class GatewayRoutePrewarmPlan(ContractModel):
    route: str
    desired_instances: int
    reason: RoutePrewarmReason = RoutePrewarmReason.PublicRoute
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


def dial_target_from_endpoint(endpoint: str, *, path: str = "/") -> BackendDialTarget:
    address = resolve_endpoint(endpoint)
    return BackendDialTarget(
        scheme=_scheme_to_protocol(address),
        host=address.host,
        port=address.port,
        path=path,
    )


def build_backend_dial_plan(
    endpoint: str,
    *,
    path: str = "/",
    tailnet_peer: TailnetPeer | None = None,
    headers: dict[str, str] | None = None,
    metadata: dict[str, JsonValue] | None = None,
) -> BackendDialPlan:
    return BackendDialPlan(
        target=dial_target_from_endpoint(endpoint, path=path),
        via_tailnet_peer=tailnet_peer.name if tailnet_peer else None,
        headers=headers or {},
        metadata=metadata or {},
    )


def build_backend_route_dial_plan(
    route_id: str,
    *,
    path: str = "/",
    headers: dict[str, str] | None = None,
    metadata: dict[str, JsonValue] | None = None,
) -> BackendDialPlan:
    normalized_route_id = _normalized_route_id(route_id)
    route_metadata = dict(metadata or {})
    route_metadata[BACKEND_ROUTE_ID_METADATA_KEY] = normalized_route_id
    return BackendDialPlan(
        target=BackendDialTarget(
            scheme=GatewayProtocol.Tcp,
            host=BACKEND_ROUTE_DIAL_HOST,
            path=path,
        ),
        headers=headers or {},
        metadata=route_metadata,
    )


def backend_route_preface(route_id: str, credential: str) -> bytes:
    normalized_route_id = _normalized_route_id(route_id)
    normalized_credential = credential.strip()
    if not normalized_credential or any(character.isspace() for character in normalized_credential):
        raise ValueError("backend route credential is required")
    return f"{BACKEND_ROUTE_PREFACE}{normalized_route_id} {normalized_credential}\n".encode()


def tailnet_resolve_preface(host: str, credential: str) -> bytes:
    normalized_host = host.strip().rstrip(".")
    normalized_credential = credential.strip()
    if not normalized_host or any(character.isspace() for character in normalized_host):
        raise ValueError("tailnet resolve host is required")
    if not normalized_credential or any(character.isspace() for character in normalized_credential):
        raise ValueError("tailnet resolve credential is required")
    return f"{TAILNET_RESOLVE_PREFACE}{normalized_host} {normalized_credential}\n".encode()


def parse_tailnet_resolve_preface(line: str) -> tuple[str, str] | None:
    if not line.startswith(TAILNET_RESOLVE_PREFACE):
        return None
    fields = line.removeprefix(TAILNET_RESOLVE_PREFACE).split()
    if len(fields) != 2:
        return None
    host = fields[0].strip().rstrip(".")
    if not host:
        return None
    return (host, fields[1])


def parse_backend_route_preface(line: str) -> tuple[str, str] | None:
    if not line.startswith(BACKEND_ROUTE_PREFACE):
        return None
    fields = line.removeprefix(BACKEND_ROUTE_PREFACE).split()
    if len(fields) != 2:
        return None
    try:
        route_id = _normalized_route_id(fields[0])
    except ValueError:
        return None
    return (route_id, fields[1])


def _normalized_route_id(route_id: str) -> str:
    normalized = route_id.strip()
    if not normalized or any(character.isspace() for character in normalized):
        raise ValueError("backend route id is required")
    return normalized


def parse_subdomain_route(host: str, root_domain: str) -> SubdomainRoute:
    normalized_host = host.split(":", 1)[0].strip(".").lower()
    normalized_root = root_domain.strip(".").lower()
    suffix = f".{normalized_root}"
    if not normalized_host.endswith(suffix):
        msg = f"host {host!r} is not under root domain {root_domain!r}"
        raise ValueError(msg)
    subdomain = normalized_host[: -len(suffix)]
    if not subdomain:
        msg = "host does not contain an app subdomain"
        raise ValueError(msg)
    parts = subdomain.split(".")
    app = parts[0]
    workspace = parts[1] if len(parts) > 1 else None
    return SubdomainRoute(
        host=normalized_host,
        root_domain=normalized_root,
        subdomain=subdomain,
        app=app,
        workspace=workspace,
    )


def plan_route_prewarm(
    route: str,
    *,
    public: bool,
    keep_warm: int = 0,
) -> GatewayRoutePrewarmPlan:
    if keep_warm > 0:
        desired = keep_warm
        reason = RoutePrewarmReason.KeepWarm
    elif public:
        desired = 1
        reason = RoutePrewarmReason.PublicRoute
    else:
        desired = 0
        reason = RoutePrewarmReason.Manual
    return GatewayRoutePrewarmPlan(route=route, desired_instances=desired, reason=reason)


def _scheme_to_protocol(address: EndpointAddress) -> GatewayProtocol:
    if address.scheme == "https":
        return GatewayProtocol.Https
    if address.scheme == "tcp":
        return GatewayProtocol.Tcp
    return GatewayProtocol.Http
