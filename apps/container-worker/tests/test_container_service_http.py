from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field

from container_worker_app.container_service_http import create_container_service_app
from fastapi.testclient import TestClient
from worker.container_client.models import (
    ContainerLogEntry,
    ContainerSandboxUploadFileRequest,
    ContainerSandboxUploadFileResponse,
    ContainerServiceMethod,
    ContainerServicePayload,
    ContainerStreamLogsRequest,
)
from worker.container_client.wire import encode_container_service_wire_value


@dataclass(slots=True)
class _RecordingHandler:
    unary_requests: list[ContainerSandboxUploadFileRequest] = field(default_factory=list)
    stream_requests: list[ContainerStreamLogsRequest] = field(default_factory=list)

    def unary(
        self,
        method: ContainerServiceMethod,
        request: ContainerServicePayload,
        *,
        timeout_seconds: float | None = None,
    ) -> ContainerServicePayload:
        assert timeout_seconds is None
        assert method is ContainerServiceMethod.ContainerSandboxUploadFile
        parsed = ContainerSandboxUploadFileRequest.model_validate(request)
        self.unary_requests.append(parsed)
        return ContainerSandboxUploadFileResponse()

    def stream(
        self,
        method: ContainerServiceMethod,
        request: ContainerServicePayload,
        *,
        timeout_seconds: float | None = None,
    ) -> Generator[ContainerServicePayload, None, None]:
        assert timeout_seconds is None
        assert method is ContainerServiceMethod.ContainerStreamLogs
        parsed = ContainerStreamLogsRequest.model_validate(request)
        self.stream_requests.append(parsed)
        yield ContainerLogEntry(msg="first")
        yield ContainerLogEntry(msg="second")


def test_container_service_http_validates_auth_and_decodes_binary_requests() -> None:
    handler = _RecordingHandler()
    client = TestClient(create_container_service_app(handler, token="worker-token"))
    request = ContainerSandboxUploadFileRequest(
        container_id="container-one",
        container_path="/workspace/blob",
        data=b"\xff\x00",
    )

    denied = client.post(
        "/container-service/ContainerSandboxUploadFile",
        json=encode_container_service_wire_value(request),
    )
    accepted = client.post(
        "/container-service/ContainerSandboxUploadFile",
        json=encode_container_service_wire_value(request),
        headers={"authorization": "Bearer worker-token"},
    )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True, "error_msg": ""}
    assert handler.unary_requests == [request]
