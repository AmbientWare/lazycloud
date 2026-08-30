from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field, JsonValue, SecretStr, computed_field, field_validator
from shared.contracts import ContractModel

BACKEND_ROUTE_DIAL_HOST = "backend.route"
BACKEND_ROUTE_ID_METADATA_KEY = "backend_route_id"
BACKEND_ROUTE_PREFACE = "BACKEND-ROUTE/2 "
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
    headers: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


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
