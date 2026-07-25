from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

from shared.contracts import ContractModel
from shared.http.client_manifests import ClientManifestRequest, ClientManifestResponse
from shared.http.gateway import (
    AttachToContainerRequest,
    AttachToContainerResponse,
    CheckpointContainerRequest,
    CheckpointContainerResponse,
    DeployStubRequest,
    DeployStubResponse,
    GetOrCreateStubRequest,
    GetOrCreateStubResponse,
    GetUrlRequest,
    GetUrlResponse,
    ResolveDeploymentTargetRequest,
    ResolveDeploymentTargetResponse,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from shared.http_transport import HttpChannel


class GatewayControlChannel(Protocol):
    def get(self, path: str) -> Any: ...

    def stream_get(self, path: str) -> Iterator[str]: ...

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...


@dataclass
class GatewayControlClient:
    channel: GatewayControlChannel

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> GatewayControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds)
        )

    def checkpoint_container(
        self,
        request: CheckpointContainerRequest,
    ) -> CheckpointContainerResponse:
        return CheckpointContainerResponse.model_validate(
            self.channel.post("/gateway/containers/checkpoint", _payload(request))
        )

    def attach_to_container(self, container_id: str) -> dict[str, Any]:
        return self.attach_to_container_response(container_id).model_dump(mode="json")

    def attach_to_container_response(self, container_id: str) -> AttachToContainerResponse:
        response = self.channel.post(
            "/gateway/containers/attach",
            AttachToContainerRequest(container_id=container_id).model_dump(mode="json"),
        )
        return AttachToContainerResponse.model_validate(response)

    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> Iterator[AttachToContainerResponse]:
        query = urlencode(
            {
                "container_id": container_id,
                "poll_interval_seconds": poll_interval_seconds,
            }
        )
        for event, payload in _sse_json_events(
            self.channel.stream_get(f"/gateway/containers/attach/stream?{query}")
        ):
            if event in {"output", "done"}:
                yield AttachToContainerResponse.model_validate(payload)

    def sync_container_workspace(
        self,
        body: SyncContainerWorkspaceBody,
    ) -> SyncContainerWorkspaceResponse:
        return SyncContainerWorkspaceResponse.model_validate(
            self.channel.post("/gateway/containers/sync-workspace", _payload(body))
        )

    def get_or_create_stub(
        self,
        request: GetOrCreateStubRequest,
    ) -> GetOrCreateStubResponse:
        return GetOrCreateStubResponse.model_validate(
            self.channel.post("/gateway/stubs/get-or-create", _payload(request))
        )

    def deploy_stub(self, request: DeployStubRequest) -> DeployStubResponse:
        return DeployStubResponse.model_validate(
            self.channel.post("/gateway/stubs/deploy", _payload(request))
        )

    def get_url(self, request: GetUrlRequest) -> GetUrlResponse:
        return GetUrlResponse.model_validate(
            self.channel.post("/gateway/stubs/url", _payload(request))
        )

    def resolve_deployment_target(
        self,
        request: ResolveDeploymentTargetRequest,
    ) -> ResolveDeploymentTargetResponse:
        return ResolveDeploymentTargetResponse.model_validate(
            self.channel.post("/gateway/deployments/resolve-target", _payload(request))
        )

    def client_manifest(self, request: ClientManifestRequest) -> ClientManifestResponse:
        return ClientManifestResponse.model_validate(
            self.channel.post("/gateway/client-manifests", _payload(request))
        )


def _payload(request: ContractModel) -> dict[str, Any]:
    return request.model_dump(mode="json", exclude_none=True)


def _sse_json_events(lines: Iterator[str]) -> Iterator[tuple[str, object]]:
    event = ""
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            if data_lines:
                yield event, json.loads("\n".join(data_lines))
            event = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)


__all__ = [
    "GatewayControlChannel",
    "GatewayControlClient",
]
