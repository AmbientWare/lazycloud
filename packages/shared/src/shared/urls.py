from __future__ import annotations

from urllib.parse import ParseResult, quote, urlparse, urlsplit, urlunparse

from pydantic import Field

from shared.contracts import ContractModel
from shared.deployment_subdomains import deployment_host_label
from shared.deployments import StubKind


def tcp_ingress_hostname(stub_id: str, port: int, external_host: str) -> str:
    normalized_stub = stub_id.strip().lower()
    normalized_host = external_host.strip(".").lower()
    if not normalized_stub or not normalized_host:
        raise ValueError("stub id and TCP ingress external host are required")
    if not 1 <= port <= 65535:
        raise ValueError("TCP ingress port must be between 1 and 65535")
    return f"{normalized_stub}-{port}.{normalized_host}"


class StubUrlTarget(ContractModel):
    kind: str
    stub_id: str
    deployment_name: str = ""
    deployment_version: int = 1
    subdomain: str = ""
    public: bool = False
    ports: list[int] = Field(default_factory=list)
    route: str | None = None

    @property
    def invoke_path(self) -> str:
        if self.kind != StubKind.Endpoint.value:
            return ""
        path = endpoint_route_path(self.route)
        return path if path != "/" else ""


def endpoint_route_path(route: str | None) -> str:
    return "/" + (route or "").lstrip("/")


def url_path_segment(value: str) -> str:
    """Escape one value so it occupies exactly one path segment of a REST URL."""

    return quote(value, safe="")


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


def normalize_return_path(value: str, *, field_name: str = "return path") -> str:
    """A same-origin path a browser may be sent back to after signing in.

    Only a path, never a URL. Anything that could name a host — a scheme, a
    protocol-relative `//host`, a backslash some browsers normalize to a slash —
    would turn the sign-in route into an open redirect that borrows this origin's
    credibility to land somebody on an attacker's page.
    """
    normalized = value.strip()
    if not normalized:
        return ""
    if len(normalized) > 512:
        raise ValueError(f"{field_name} is too long")
    if any(character.isspace() or ord(character) < 0x20 for character in normalized):
        raise ValueError(f"{field_name} must not contain whitespace or control characters")
    if "\x00" in normalized:
        raise ValueError(f"{field_name} must not contain a null byte")
    if not normalized.startswith("/") or normalized.startswith(("//", "/\\")):
        raise ValueError(f"{field_name} must be a path beginning with a single '/'")
    if "\\" in normalized:
        raise ValueError(f"{field_name} must not contain a backslash")
    path = urlsplit(normalized).path
    if any(segment == ".." for segment in path.split("/")):
        raise ValueError(f"{field_name} must not contain a '..' segment")
    return normalized


def build_deployment_url(
    external_url: str,
    target: StubUrlTarget,
    *,
    pin_version: bool = False,
) -> str:
    """The hostname a deployed resource answers on.

    A hostname rather than a path under the origin because an application has to own
    the root to work: a page served beneath `/api/v1/asgi/<name>/` still emits its own
    absolute links and asset paths against `/`, and nothing on the server can rewrite
    what a bundler already baked into the files.

    Unpinned by default, so the URL a caller publishes keeps working across redeploys.
    """

    parsed = _parse_external_url(external_url)
    if not target.subdomain:
        raise ValueError("deployment URL requires the deployment's subdomain")
    label = deployment_host_label(
        target.subdomain,
        version=target.deployment_version if pin_version else None,
    )
    return _replace_host(parsed, f"{label}.{parsed.netloc}", path=target.invoke_path)


def deployment_handler_path(
    kind: str, name: str, *, version: int | None = None, route: str | None = None
) -> str:
    """Where the platform itself serves a deployed resource.

    The address a public hostname is rewritten onto, and the one a first-party
    caller uses directly. Same-origin, so a browser client the platform serves does
    not need permission from the resource to call it.
    """

    prefix = handler_prefix(kind)
    suffix = "latest" if version is None else f"v{version}"
    path = endpoint_route_path(route) if kind == StubKind.Endpoint.value else "/"
    return f"/{prefix}/{url_path_segment(name)}/{suffix}" + (
        quote(path, safe="/") if path != "/" else ""
    )


def handler_prefix(kind: str) -> str:
    if kind == "function":
        return "api/v1/functions"
    if kind == "endpoint":
        return "api/v1/endpoints"
    if kind == "asgi":
        return "api/v1/asgi"
    return kind


def build_stub_url(external_url: str, target: StubUrlTarget) -> str:
    parsed = _parse_external_url(external_url)
    return _replace_host(parsed, f"{target.stub_id}.{parsed.netloc}", path=target.invoke_path)


def build_container_url(external_url: str, container_id: str, *, path: str = "") -> str:
    parsed = _parse_external_url(external_url)
    return _replace_host(parsed, f"{container_id}.{parsed.netloc}", path=path)


def build_pod_url(external_url: str, target: StubUrlTarget) -> str:
    parsed = _parse_external_url(external_url)
    port = str(target.ports[0]) if len(target.ports) == 1 else "<PORT>"
    return _replace_host(parsed, f"{target.stub_id}-{port}.{parsed.netloc}")


def pod_proxy_url(
    gateway_http_url: str,
    *,
    resource: StubKind,
    stub_id: str,
    port: int,
    container_id: str | None = None,
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
    if container_id:
        return _replace_host(parsed, f"{container_id}-{port}.{parsed.netloc}")
    if resource is StubKind.Sandbox:
        msg = "container id is required for sandbox proxy URLs"
        raise ValueError(msg)
    return _replace_host(parsed, f"{stub_id}-{port}.{parsed.netloc}")


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


def _replace_host(parsed: ParseResult, host: str, *, path: str = "") -> str:
    return urlunparse((parsed.scheme, host, quote(path, safe="/"), "", "", ""))
