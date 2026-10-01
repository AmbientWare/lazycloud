"""Instances, container processes, files, ports, network, SSH and shells through the public API.

Container operations reach the container's supervisor through the platform, so
they answer only while the container runs.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from uuid import UUID

import httpx
from shared.api import (
    ContainerFile,
    ContainerFileList,
    ContainerLifecycle,
    ContainerLogEntry,
    ContainerPort,
    ContainerPortList,
    CreateInstanceRequest,
    Devbox,
    ExposePortRequest,
    FileMatches,
    FilesystemImage,
    FindInFilesRequest,
    Instance,
    KillRequest,
    MemorySnapshot,
    NetworkPolicy,
    PodRole,
    Process,
    ProcessList,
    ProcessRequest,
    ReplacedFiles,
    ReplaceInFilesRequest,
    SandboxPage,
    SandboxStats,
    SnapshotRequest,
    SshCertificate,
    SshCertificateRequest,
    SshHostPage,
    Ttl,
    TtlRequest,
)

from lazycloud.clients.api import ApiClient, ApiConnectionError, _api_error, _path, _query

# Added to a long poll's hold so the read does not time out first.
_WAIT_MARGIN_SECONDS = 15.0
# Snapshots and filesystem images answer once they are stored.
_PUBLISH_TIMEOUT_SECONDS = 900.0
_FILE_TRANSFER_TIMEOUT = 600.0
# The header a truncated download carries.
TRUNCATED_HEADER = "Lazycloud-Truncated"
# The longest connectContainer and getProcess holds the API serves.
CONNECT_WAIT_SECONDS = 60
PROCESS_WAIT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class FileContent:
    data: bytes
    truncated: bool


@dataclass
class WorkloadsClient:
    """The workload operations of one workspace."""

    api: ApiClient
    workspace: str

    # Instances

    def create_instance(self, request: CreateInstanceRequest) -> Instance:
        return self.api._send(Instance, "POST", self._path("instances"), body=request)

    def connect(self, container_id: UUID, *, wait_seconds: int = 0) -> Instance:
        """The container once it is ready; `unavailable` while it starts, `conflict` once stopped."""
        return self.api._send(
            Instance,
            "POST",
            self._container(container_id, "connect"),
            params={"wait_seconds": wait_seconds} if wait_seconds else None,
            read_timeout=wait_seconds + _WAIT_MARGIN_SECONDS if wait_seconds else None,
        )

    def stream_output(
        self, container_id: UUID, *, after: int = 0, follow: bool = False
    ) -> Iterator[ContainerLogEntry]:
        """What the container wrote outside task attempts; following ends once it stops."""
        return self.api._stream_lines(
            ContainerLogEntry,
            self._container(container_id, "output"),
            after=after,
            tail=None,
            follow=follow,
        )

    def lifecycle(self, container_id: UUID) -> ContainerLifecycle:
        return self.api._send(
            ContainerLifecycle, "GET", self._container(container_id, "lifecycle")
        )

    # Processes

    def start_process(self, container_id: UUID, request: ProcessRequest) -> Process:
        return self.api._send(
            Process, "POST", self._container(container_id, "processes"), body=request
        )

    def get_process(
        self, container_id: UUID, process_id: str, *, wait_seconds: float = 0
    ) -> Process:
        return self.api._send(
            Process,
            "GET",
            self._container(container_id, "processes", process_id),
            params={"wait_seconds": _seconds(wait_seconds)} if wait_seconds > 0 else None,
            read_timeout=wait_seconds + _WAIT_MARGIN_SECONDS if wait_seconds > 0 else None,
        )

    def kill_process(self, container_id: UUID, process_id: str) -> None:
        self.api._send(
            None,
            "POST",
            self._container(container_id, "processes", process_id, "kill"),
            body=KillRequest(),
        )

    def list_processes(self, container_id: UUID) -> ProcessList:
        return self.api._send(ProcessList, "GET", self._container(container_id, "processes"))

    # Files

    def list_files(self, container_id: UUID, path: str) -> ContainerFileList:
        return self.api._send(
            ContainerFileList,
            "GET",
            self._container(container_id, "files"),
            params={"path": path},
        )

    def stat_file(self, container_id: UUID, path: str) -> ContainerFile:
        return self.api._send(
            ContainerFile,
            "GET",
            self._container(container_id, "files", "stat"),
            params={"path": path},
        )

    def delete_file(self, container_id: UUID, path: str) -> None:
        self.api._send(
            None, "DELETE", self._container(container_id, "files"), params={"path": path}
        )

    def download_file(self, container_id: UUID, path: str) -> FileContent:
        response = self._raw(
            "GET", self._container(container_id, "files", "content"), params={"path": path}
        )
        return FileContent(
            data=response.content,
            truncated=response.headers.get(TRUNCATED_HEADER, "").lower() == "true",
        )

    def upload_file(
        self, container_id: UUID, path: str, content: bytes, *, mode: int = 0o644
    ) -> None:
        """Write a file and its parent directories; the server replaces it atomically."""
        self._raw(
            "PUT",
            self._container(container_id, "files", "content"),
            params={"path": path, "mode": mode},
            content=content,
        )

    def find_in_files(self, container_id: UUID, request: FindInFilesRequest) -> FileMatches:
        return self.api._send(
            FileMatches, "POST", self._container(container_id, "files", "find"), body=request
        )

    def replace_in_files(
        self, container_id: UUID, request: ReplaceInFilesRequest
    ) -> ReplacedFiles:
        return self.api._send(
            ReplacedFiles,
            "POST",
            self._container(container_id, "files", "replace"),
            body=request,
        )

    def create_directory(self, container_id: UUID, path: str, *, mode: int = 0o755) -> None:
        self.api._send(
            None,
            "POST",
            self._container(container_id, "directories"),
            params={"path": path, "mode": mode},
        )

    def delete_directory(self, container_id: UUID, path: str) -> None:
        self.api._send(
            None, "DELETE", self._container(container_id, "directories"), params={"path": path}
        )

    # Ports, network and lifetime

    def expose_port(self, container_id: UUID, port: int) -> ContainerPort:
        return self.api._send(
            ContainerPort,
            "POST",
            self._container(container_id, "ports"),
            body=ExposePortRequest(port=port),
        )

    def list_ports(self, container_id: UUID) -> ContainerPortList:
        return self.api._send(ContainerPortList, "GET", self._container(container_id, "ports"))

    def network(self, container_id: UUID) -> NetworkPolicy:
        return self.api._send(NetworkPolicy, "GET", self._container(container_id, "network"))

    def set_network(self, container_id: UUID, policy: NetworkPolicy) -> NetworkPolicy:
        return self.api._send(
            NetworkPolicy, "PUT", self._container(container_id, "network"), body=policy
        )

    def set_ttl(self, container_id: UUID, ttl: int) -> Ttl:
        return self.api._send(
            Ttl, "PUT", self._container(container_id, "ttl"), body=TtlRequest(ttl=ttl)
        )

    def snapshot(self, container_id: UUID, *, snapshot_id: UUID | None = None) -> MemorySnapshot:
        return self.api._send(
            MemorySnapshot,
            "POST",
            self._container(container_id, "snapshots"),
            body=SnapshotRequest(snapshot_id=snapshot_id) if snapshot_id is not None else None,
            read_timeout=_PUBLISH_TIMEOUT_SECONDS,
        )

    def create_filesystem_image(self, container_id: UUID) -> FilesystemImage:
        return self.api._send(
            FilesystemImage,
            "POST",
            self._container(container_id, "filesystem-images"),
            read_timeout=_PUBLISH_TIMEOUT_SECONDS,
        )

    # Sandboxes

    def list_sandboxes(
        self, *, app: str | None = None, limit: int = 100, cursor: str | None = None
    ) -> SandboxPage:
        return self.api._send(
            SandboxPage,
            "GET",
            self._path("sandboxes"),
            params=_query(app=app, limit=limit, cursor=cursor),
        )

    def sandbox_stats(self, *, app: str | None = None) -> SandboxStats:
        return self.api._send(
            SandboxStats, "GET", self._path("sandboxes", "stats"), params=_query(app=app)
        )

    # Devboxes and SSH

    def devbox(self, deployment_id: UUID) -> Devbox:
        return self.api._send(
            Devbox, "GET", self._path("deployments", str(deployment_id), "devbox")
        )

    def create_ssh_certificate(self, public_key: str) -> SshCertificate:
        return self.api._send(
            SshCertificate,
            "POST",
            self._path("ssh", "certificates"),
            body=SshCertificateRequest(public_key=public_key),
        )

    def ssh_hosts(
        self,
        *,
        app: str | None = None,
        pod: str | None = None,
        role: PodRole | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> SshHostPage:
        return self.api._send(
            SshHostPage,
            "GET",
            self._path("ssh", "hosts"),
            params=_query(
                app=app,
                pod=pod,
                role=role.value if role is not None else None,
                limit=limit,
                cursor=cursor or None,
            ),
        )

    # WebSockets

    def shell_url(self, container_id: UUID, *, cols: int, rows: int, term: str) -> str:
        query = httpx.QueryParams({"cols": cols, "rows": rows, "term": term})
        return websocket_url(self.api.endpoint, f"{self._container(container_id, 'shell')}?{query}")

    def ssh_tunnel_url(self, app: str, pod: str) -> str:
        return websocket_url(self.api.endpoint, self._path("apps", app, "pods", pod, "ssh"))

    def _raw(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int],
        content: bytes | None = None,
    ) -> httpx.Response:
        try:
            response = self.api._client().request(
                method,
                path,
                params=params,
                content=content,
                headers=(
                    {"Content-Type": "application/octet-stream"} if content is not None else None
                ),
                timeout=httpx.Timeout(self.api.timeout_seconds, read=_FILE_TRANSFER_TIMEOUT),
            )
        except httpx.HTTPError as exc:
            raise ApiConnectionError(method, path, str(exc) or type(exc).__name__) from exc
        if response.status_code >= 300:
            raise _api_error(response)
        return response

    def _container(self, container_id: UUID, *segments: str) -> str:
        return self._path("containers", str(container_id), *segments)

    def _path(self, *segments: str) -> str:
        return _path("v1", "workspaces", self.workspace, *segments)


def websocket_url(endpoint: str, path: str) -> str:
    """The ws:// or wss:// URL of an API path."""
    base = httpx.URL(endpoint.rstrip("/"))
    schemes = {"http": "ws", "https": "wss"}
    if base.scheme not in schemes:
        msg = f"unsupported API endpoint scheme: {base.scheme or '<missing>'}"
        raise ValueError(msg)
    return f"{schemes[base.scheme]}://{base.netloc.decode()}{base.path.rstrip('/')}{path}"


def _seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


__all__ = [
    "CONNECT_WAIT_SECONDS",
    "PROCESS_WAIT_SECONDS",
    "FileContent",
    "WorkloadsClient",
    "websocket_url",
]
