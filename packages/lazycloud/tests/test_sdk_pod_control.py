from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lazycloud.clients.pod.control import PodControlClient
from shared.http.pods import PodSandboxDownloadFileResponse


@dataclass
class RecordingChannel:
    gets: list[str] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        raise AssertionError(f"unexpected POST {path}: {payload}")

    def get(self, path: str) -> dict[str, str]:
        self.gets.append(path)
        return PodSandboxDownloadFileResponse.from_bytes(b"payload").model_dump(mode="json")

    def delete(self, path: str) -> dict[str, object]:
        self.deletes.append(path)
        return {}


def test_pod_file_routes_preserve_absolute_container_paths() -> None:
    channel = RecordingChannel()
    client = PodControlClient(channel, workspace="tenant-b")

    response = client.sandbox_download_file("container-1", "/workspace/a file.txt")
    client.sandbox_delete_file("container-1", "/workspace/a file.txt")
    client.sandbox_delete_directory("container-1", "/workspace/a directory")

    assert response.data == b"payload"
    assert channel.gets == [
        "/api/v1/pods/container-1/files/download"
        "?container_path=%2Fworkspace%2Fa%20file.txt&workspace=tenant-b"
    ]
    assert channel.deletes == [
        "/api/v1/pods/container-1/files"
        "?container_path=%2Fworkspace%2Fa%20file.txt&workspace=tenant-b",
        "/api/v1/pods/container-1/directories"
        "?container_path=%2Fworkspace%2Fa%20directory&workspace=tenant-b",
    ]
