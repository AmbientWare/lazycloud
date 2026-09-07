from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from shared.bytes_transport import encode_bytes
from shared.http.pods import (
    CreatePodRequest,
    CreatePodResponse,
    PodSandboxConnectResponse,
    PodSandboxCreateDirectoryRequest,
    PodSandboxCreateDirectoryResponse,
    PodSandboxCreateImageFromFilesystemRequest,
    PodSandboxCreateImageFromFilesystemResponse,
    PodSandboxDeleteDirectoryResponse,
    PodSandboxDeleteFileResponse,
    PodSandboxDownloadFileResponse,
    PodSandboxExecRequest,
    PodSandboxExecResponse,
    PodSandboxExposePortRequest,
    PodSandboxExposePortResponse,
    PodSandboxFindInFilesRequest,
    PodSandboxFindInFilesResponse,
    PodSandboxKillRequest,
    PodSandboxKillResponse,
    PodSandboxListFilesResponse,
    PodSandboxListProcessesResponse,
    PodSandboxListUrlsResponse,
    PodSandboxReplaceInFilesRequest,
    PodSandboxReplaceInFilesResponse,
    PodSandboxSnapshotMemoryRequest,
    PodSandboxSnapshotMemoryResponse,
    PodSandboxStatFileResponse,
    PodSandboxStatusResponse,
    PodSandboxStderrResponse,
    PodSandboxStdoutResponse,
    PodSandboxUpdateNetworkPermissionsRequest,
    PodSandboxUpdateNetworkPermissionsResponse,
    PodSandboxUpdateTTLRequest,
    PodSandboxUpdateTTLResponse,
    PodSandboxUploadFileBody,
    PodSandboxUploadFileResponse,
    SandboxListRequest,
    SandboxListResponse,
    SandboxStatsRequest,
    SandboxStatsResponse,
    SandboxTimeline,
    SandboxTimelineRequest,
)
from shared.http_transport import HttpChannel

from lazycloud.control import workspace_path


class PodControlChannel(Protocol):
    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...

    def get(self, path: str) -> Any: ...

    def delete(self, path: str) -> Any: ...


@dataclass
class PodControlClient:
    channel: PodControlChannel
    workspace: str = "default"
    """Workspace every call acts in, named rather than inferred.

    A user credential reaches every workspace its owner belongs to, so the request
    has to say which one; inside a container the workspace comes from the environment
    the runner pins. Either way the caller states it rather than letting the server
    pick one.
    """

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        workspace: str = "default",
        timeout_seconds: float = 10.0,
    ) -> PodControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def _scoped(self, path: str) -> str:
        return workspace_path(path, self.workspace)

    def create_pod(self, request: CreatePodRequest) -> CreatePodResponse:
        return CreatePodResponse.model_validate(
            self.channel.post(self._scoped("/api/v1/pods"), request.model_dump(mode="json"))
        )

    def sandbox_exec(
        self,
        container_id: str,
        request: PodSandboxExecRequest,
    ) -> PodSandboxExecResponse:
        return PodSandboxExecResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/exec"),
                request.model_dump(mode="json"),
            )
        )

    def sandbox_status(self, container_id: str, pid: int) -> PodSandboxStatusResponse:
        return PodSandboxStatusResponse.model_validate(
            self.channel.get(self._scoped(f"/api/v1/pods/{container_id}/status?pid={pid}"))
        )

    def sandbox_stdout(self, container_id: str, pid: int) -> PodSandboxStdoutResponse:
        return PodSandboxStdoutResponse.model_validate(
            self.channel.get(self._scoped(f"/api/v1/pods/{container_id}/stdout?pid={pid}"))
        )

    def sandbox_stderr(self, container_id: str, pid: int) -> PodSandboxStderrResponse:
        return PodSandboxStderrResponse.model_validate(
            self.channel.get(self._scoped(f"/api/v1/pods/{container_id}/stderr?pid={pid}"))
        )

    def sandbox_kill(
        self,
        container_id: str,
        request: PodSandboxKillRequest,
    ) -> PodSandboxKillResponse:
        return PodSandboxKillResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/kill"),
                request.model_dump(mode="json"),
            )
        )

    def sandbox_list_processes(self, container_id: str) -> PodSandboxListProcessesResponse:
        return PodSandboxListProcessesResponse.model_validate(
            self.channel.get(self._scoped(f"/api/v1/pods/{container_id}/processes"))
        )

    def sandbox_upload_file(
        self,
        container_id: str,
        container_path: str,
        data: bytes,
        *,
        mode: int = 0o644,
    ) -> PodSandboxUploadFileResponse:
        body = PodSandboxUploadFileBody(
            container_path=container_path,
            mode=mode,
            value_base64=encode_bytes(data),
        )
        return PodSandboxUploadFileResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/files/upload"),
                body.model_dump(mode="json"),
            )
        )

    def sandbox_download_file(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxDownloadFileResponse:
        return PodSandboxDownloadFileResponse.model_validate(
            self.channel.get(
                self._scoped(
                    f"/api/v1/pods/{container_id}/files/download"
                    f"?container_path={_query(container_path)}"
                )
            )
        )

    def sandbox_stat_file(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxStatFileResponse:
        return PodSandboxStatFileResponse.model_validate(
            self.channel.get(
                self._scoped(
                    f"/api/v1/pods/{container_id}/files/stat?container_path={_query(container_path)}"
                )
            )
        )

    def sandbox_list_files(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxListFilesResponse:
        return PodSandboxListFilesResponse.model_validate(
            self.channel.get(
                self._scoped(
                    f"/api/v1/pods/{container_id}/files?container_path={_query(container_path)}"
                )
            )
        )

    def sandbox_delete_file(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxDeleteFileResponse:
        return PodSandboxDeleteFileResponse.model_validate(
            self.channel.delete(
                self._scoped(
                    f"/api/v1/pods/{container_id}/files?container_path={_query(container_path)}"
                )
            )
        )

    def sandbox_create_directory(
        self,
        container_id: str,
        request: PodSandboxCreateDirectoryRequest,
    ) -> PodSandboxCreateDirectoryResponse:
        return PodSandboxCreateDirectoryResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/directories"),
                request.model_dump(mode="json"),
            )
        )

    def sandbox_delete_directory(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxDeleteDirectoryResponse:
        return PodSandboxDeleteDirectoryResponse.model_validate(
            self.channel.delete(
                self._scoped(
                    f"/api/v1/pods/{container_id}/directories?container_path={_query(container_path)}"
                )
            )
        )

    def sandbox_expose_port(
        self,
        container_id: str,
        request: PodSandboxExposePortRequest,
    ) -> PodSandboxExposePortResponse:
        return PodSandboxExposePortResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/ports/expose"),
                request.model_dump(mode="json"),
            )
        )

    def sandbox_update_network_permissions(
        self,
        container_id: str,
        request: PodSandboxUpdateNetworkPermissionsRequest,
    ) -> PodSandboxUpdateNetworkPermissionsResponse:
        return PodSandboxUpdateNetworkPermissionsResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/network/update"),
                request.model_dump(mode="json"),
            )
        )

    def sandbox_network_permissions(
        self,
        container_id: str,
    ) -> PodSandboxUpdateNetworkPermissionsResponse:
        return PodSandboxUpdateNetworkPermissionsResponse.model_validate(
            self.channel.get(self._scoped(f"/api/v1/pods/{container_id}/network"))
        )

    def sandbox_replace_in_files(
        self,
        container_id: str,
        request: PodSandboxReplaceInFilesRequest,
    ) -> PodSandboxReplaceInFilesResponse:
        return PodSandboxReplaceInFilesResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/files/replace"),
                request.model_dump(mode="json"),
            )
        )

    def sandbox_find_in_files(
        self,
        container_id: str,
        request: PodSandboxFindInFilesRequest,
    ) -> PodSandboxFindInFilesResponse:
        return PodSandboxFindInFilesResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/files/find"),
                request.model_dump(mode="json"),
            )
        )

    def sandbox_connect(self, container_id: str) -> PodSandboxConnectResponse:
        return PodSandboxConnectResponse.model_validate(
            self.channel.post(self._scoped(f"/api/v1/pods/{container_id}/connect"))
        )

    def sandbox_update_ttl(
        self,
        container_id: str,
        request: PodSandboxUpdateTTLRequest,
    ) -> PodSandboxUpdateTTLResponse:
        return PodSandboxUpdateTTLResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/ttl"),
                request.model_dump(mode="json"),
            )
        )

    def sandbox_terminate(self, container_id: str) -> None:
        self.channel.post(self._scoped(f"/api/v1/pods/{container_id}/terminate"))

    def sandbox_create_image_from_filesystem(
        self,
        container_id: str,
        request: PodSandboxCreateImageFromFilesystemRequest,
    ) -> PodSandboxCreateImageFromFilesystemResponse:
        return PodSandboxCreateImageFromFilesystemResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/create-image-from-filesystem"),
                request.model_dump(mode="json"),
            )
        )

    def sandbox_snapshot_memory(
        self,
        container_id: str,
        request: PodSandboxSnapshotMemoryRequest,
    ) -> PodSandboxSnapshotMemoryResponse:
        return PodSandboxSnapshotMemoryResponse.model_validate(
            self.channel.post(
                self._scoped(f"/api/v1/pods/{container_id}/snapshot-memory"),
                request.model_dump(mode="json"),
            )
        )

    def sandbox_list_urls(self, container_id: str) -> PodSandboxListUrlsResponse:
        return PodSandboxListUrlsResponse.model_validate(
            self.channel.get(self._scoped(f"/api/v1/pods/{container_id}/urls"))
        )

    def sandbox_list(self, request: SandboxListRequest | None = None) -> SandboxListResponse:
        selected = request or SandboxListRequest()
        query = _query_params({"app_id": selected.app_id, "limit": selected.limit})
        return SandboxListResponse.model_validate(
            self.channel.get(self._scoped(f"/api/v1/stubs/sandboxes{query}"))
        )

    def sandbox_stats(self, request: SandboxStatsRequest | None = None) -> SandboxStatsResponse:
        selected = request or SandboxStatsRequest()
        query = _query_params({"app_id": selected.app_id})
        return SandboxStatsResponse.model_validate(
            self.channel.get(self._scoped(f"/api/v1/stubs/sandboxes/stats{query}"))
        )

    def sandbox_timeline(self, request: SandboxTimelineRequest) -> SandboxTimeline:
        query = _query_params({"container_id": request.container_id})
        return SandboxTimeline.model_validate(
            self.channel.get(
                self._scoped(f"/api/v1/stubs/sandboxes/{_path(request.stub_id)}/timeline{query}")
            )
        )


def _path(value: str) -> str:
    return quote(value.strip("/") or ".", safe="/")


def _query(value: str) -> str:
    return quote(value, safe="")


def _query_params(values: dict[str, object | None]) -> str:
    query = urlencode({key: value for key, value in values.items() if value is not None})
    return f"?{query}" if query else ""


__all__ = [
    "PodControlChannel",
    "PodControlClient",
]
