"""API calls for HTTP workloads, previews and custom domains."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import httpx
from shared.api import (
    ContainerLogEntry,
    ContainerLogList,
    Domain,
    DomainList,
    DomainRequest,
    HttpRequest,
    HttpRequestList,
    HttpWorkload,
    Preview,
    PreviewRequest,
    PreviewSync,
)

from lazycloud.clients.api import ApiClient, ApiConnectionError, _api_error, _path

# The longest wait the API holds a preview read for a ready container.
PREVIEW_WAIT_SECONDS = 60
# Added to a long poll's hold so the read does not time out first.
_WAIT_MARGIN_SECONDS = 15.0
_SYNC_TIMEOUT_SECONDS = 300.0


def get_http_workload(
    client: ApiClient,
    workspace: str,
    app: str,
    name: str,
    *,
    asgi: bool,
    version: int | None = None,
) -> HttpWorkload:
    """An endpoint, or an ASGI or realtime app, and the URLs it answers on."""

    collection = "asgi" if asgi else "endpoints"
    return client._send(
        HttpWorkload,
        "GET",
        _path("v1", "workspaces", workspace, "apps", app, collection, name),
        params={"version": version} if version is not None else None,
    )


def create_preview(client: ApiClient, workspace: str, app: str, request: PreviewRequest) -> Preview:
    return client._send(
        Preview, "POST", _path("v1", "workspaces", workspace, "apps", app, "previews"), body=request
    )


def get_preview(
    client: ApiClient, workspace: str, preview: UUID, *, wait_seconds: int = 0
) -> Preview:
    return client._send(
        Preview,
        "GET",
        _path("v1", "workspaces", workspace, "previews", str(preview)),
        params={"wait_seconds": wait_seconds} if wait_seconds else None,
        read_timeout=wait_seconds + _WAIT_MARGIN_SECONDS if wait_seconds else None,
    )


def stop_preview(client: ApiClient, workspace: str, preview: UUID) -> Preview:
    return client._send(
        Preview, "DELETE", _path("v1", "workspaces", workspace, "previews", str(preview))
    )


def sync_preview_files(
    client: ApiClient, workspace: str, preview: UUID, archive: bytes
) -> PreviewSync:
    """Send a tar of changed and removed files to the preview's container."""

    path = _path("v1", "workspaces", workspace, "previews", str(preview), "files")
    try:
        response = client._client().post(
            path,
            content=archive,
            headers={"Content-Type": "application/octet-stream"},
            timeout=httpx.Timeout(client.timeout_seconds, write=_SYNC_TIMEOUT_SECONDS),
        )
    except httpx.HTTPError as exc:
        raise ApiConnectionError("POST", path, str(exc) or type(exc).__name__) from exc
    if response.status_code >= 300:
        raise _api_error(response)
    return PreviewSync.model_validate_json(response.content)


def stream_preview_output(
    client: ApiClient, workspace: str, preview: UUID, *, after: int = 0, follow: bool = False
) -> Iterator[ContainerLogEntry]:
    """Yield the preview container's output; following keeps the preview alive."""

    path = _path("v1", "workspaces", workspace, "previews", str(preview), "output")
    return client._stream_lines(ContainerLogEntry, path, after=after, tail=None, follow=follow)


def list_http_requests(
    client: ApiClient,
    workspace: str,
    app: str,
    *,
    name: str | None = None,
    limit: int = 50,
) -> list[HttpRequest]:
    """An app's endpoint and ASGI requests, newest first, up to `limit`."""

    requests: list[HttpRequest] = []
    before = ""
    while len(requests) < limit:
        params: dict[str, str | int] = {"limit": min(200, limit - len(requests))}
        if name:
            params["name"] = name
        if before:
            params["before"] = before
        page = client._send(
            HttpRequestList,
            "GET",
            _path("v1", "workspaces", workspace, "apps", app, "requests"),
            params=params,
        )
        requests.extend(page.data)
        if page.next is None:
            break
        before = str(page.next)
    return requests


def http_request_logs(client: ApiClient, workspace: str, request: UUID) -> list[ContainerLogEntry]:
    """What the workload wrote while serving the request, in order."""

    entries: list[ContainerLogEntry] = []
    after = 0
    while True:
        page = client._send(
            ContainerLogList,
            "GET",
            _path("v1", "workspaces", workspace, "requests", str(request), "logs"),
            params={"after": after, "limit": 1000},
        )
        entries.extend(page.data)
        if len(page.data) < 1000:
            return entries
        after = page.data[-1].id


def register_domain(client: ApiClient, hostname: str) -> Domain:
    return client._send(Domain, "POST", "/v1/domains", body=DomainRequest(hostname=hostname))


def list_domains(client: ApiClient) -> list[Domain]:
    domains: list[Domain] = []
    after = ""
    while True:
        params: dict[str, str | int] = {"limit": 100}
        if after:
            params["after"] = after
        page = client._send(DomainList, "GET", "/v1/domains", params=params)
        domains.extend(page.data)
        if not page.next:
            return domains
        after = page.next


def get_domain(client: ApiClient, hostname: str) -> Domain:
    return client._send(Domain, "GET", _path("v1", "domains", hostname))


def remove_domain(client: ApiClient, hostname: str) -> None:
    path = _path("v1", "domains", hostname)
    try:
        response = client._client().delete(path)
    except httpx.HTTPError as exc:
        raise ApiConnectionError("DELETE", path, str(exc) or type(exc).__name__) from exc
    if response.status_code >= 300:
        raise _api_error(response)


__all__ = [
    "PREVIEW_WAIT_SECONDS",
    "create_preview",
    "get_domain",
    "get_http_workload",
    "get_preview",
    "http_request_logs",
    "list_domains",
    "list_http_requests",
    "register_domain",
    "remove_domain",
    "stop_preview",
    "stream_preview_output",
    "sync_preview_files",
]
