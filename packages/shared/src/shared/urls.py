from __future__ import annotations

from urllib.parse import ParseResult, urlparse, urlsplit, urlunparse

from pydantic import Field

from shared.contracts import ContractModel
from shared.deployments import StubKind
from shared.enums import StringEnum


class InvokeUrlMode(StringEnum):
    Path = "path"
    Host = "host"


class StubUrlTarget(ContractModel):
    kind: str
    stub_id: str
    deployment_name: str = ""
    deployment_version: int = 1
    deployment_subdomain: str = ""
    public: bool = False
    ports: list[int] = Field(default_factory=list)


def normalize_http_origin(value: str, *, field_name: str = "HTTP origin") -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if any(character.isspace() for character in normalized):
        raise ValueError(f"{field_name} must not contain whitespace")
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} has an invalid host or port") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"{field_name} must use http or https")
    if not parsed.hostname:
        raise ValueError(f"{field_name} must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain credentials")
    if parsed.path not in {"", "/"}:
        raise ValueError(f"{field_name} must not contain a path")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not contain a query or fragment")
    if port == 0:
        raise ValueError(f"{field_name} has an invalid port")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port_suffix = f":{port}" if port is not None else ""
    return f"{scheme}://{host}{port_suffix}"


def build_deployment_url(
    external_url: str,
    mode: InvokeUrlMode,
    target: StubUrlTarget,
) -> str:
    parsed = _parse_external_url(external_url)
    if mode == InvokeUrlMode.Host:
        subdomain = target.deployment_subdomain or target.deployment_name or target.stub_id
        return _replace_host(parsed, f"{subdomain}-v{target.deployment_version}.{parsed.netloc}")
    path_kind = _deployed_path_kind(target.kind)
    if target.public:
        return _replace_path(parsed, f"/{path_kind}/public/{target.stub_id}")
    return _replace_path(
        parsed,
        f"/{path_kind}/{target.deployment_name}/v{target.deployment_version}",
    )


def build_stub_url(external_url: str, mode: InvokeUrlMode, target: StubUrlTarget) -> str:
    parsed = _parse_external_url(external_url)
    if mode == InvokeUrlMode.Host:
        return _replace_host(parsed, f"{target.stub_id}.{parsed.netloc}")
    return _replace_path(parsed, f"/{_deployed_path_kind(target.kind)}/id/{target.stub_id}")


def build_pod_url(external_url: str, mode: InvokeUrlMode, target: StubUrlTarget) -> str:
    parsed = _parse_external_url(external_url)
    port = str(target.ports[0]) if len(target.ports) == 1 else "<PORT>"
    if mode == InvokeUrlMode.Host:
        return _replace_host(parsed, f"{target.stub_id}-{port}.{parsed.netloc}")
    if target.public:
        return _replace_path(parsed, f"/{target.kind}/public/{target.stub_id}/{port}")
    return _replace_path(parsed, f"/{target.kind}/id/{target.stub_id}/{port}")


def pod_proxy_url(
    gateway_http_url: str,
    *,
    resource: StubKind,
    stub_id: str,
    port: int,
    public: bool,
    container_id: str | None = None,
    mode: InvokeUrlMode = InvokeUrlMode.Path,
) -> str:
    if gateway_http_url != gateway_http_url.strip():
        raise ValueError("gateway HTTP URL must not contain surrounding whitespace")
    origin = normalize_http_origin(gateway_http_url, field_name="gateway HTTP URL")
    parsed = urlparse(origin)
    if resource not in {StubKind.Pod, StubKind.Sandbox}:
        msg = f"unsupported pod proxy resource: {resource}"
        raise ValueError(msg)
    if not stub_id:
        msg = "stub id is required for pod proxy URLs"
        raise ValueError(msg)
    if isinstance(port, bool) or not 1 <= port <= 65535:
        msg = "pod proxy port must be between 1 and 65535"
        raise ValueError(msg)
    if resource is StubKind.Sandbox:
        if not container_id:
            msg = "container id is required for sandbox proxy URLs"
            raise ValueError(msg)
        if mode is InvokeUrlMode.Host:
            return _replace_host(parsed, f"{container_id}-{port}.{parsed.netloc}")
        access = "public" if public else "id"
        return f"{origin}/{StubKind.Sandbox.value}/{access}/{container_id}/{port}"
    if container_id is not None:
        msg = "container id is supported only for sandbox proxy URLs"
        raise ValueError(msg)
    if mode is InvokeUrlMode.Host:
        return _replace_host(parsed, f"{stub_id}-{port}.{parsed.netloc}")
    access = "public" if public else "id"
    return f"{origin}/{resource.value}/{access}/{stub_id}/{port}"


def parse_container_address(address: str, *, resource: str) -> ParseResult:
    raw = address.strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if parsed.hostname is None or parsed.port is None:
        msg = f"invalid {resource} container address: {address}"
        raise ValueError(msg)
    return parsed


def _parse_external_url(external_url: str) -> ParseResult:
    parsed = urlparse(external_url)
    if not parsed.scheme or not parsed.netloc:
        msg = "external_url must include a scheme and host"
        raise ValueError(msg)
    return parsed


def _replace_path(parsed: ParseResult, path: str) -> str:
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _replace_host(parsed: ParseResult, host: str) -> str:
    return urlunparse((parsed.scheme, host, "", "", "", ""))


def _deployed_path_kind(kind: str) -> str:
    if kind == "task-queue":
        return "api/v1/taskqueues"
    if kind == "function":
        return "api/v1/functions"
    if kind == "endpoint":
        return "api/v1/endpoints"
    if kind == "asgi":
        return "api/v1/asgi"
    return kind
