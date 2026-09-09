from __future__ import annotations

from collections.abc import Iterator, Mapping

from pydantic import JsonValue
from shared.compute_policy import MachinePool
from shared.scheduling import WorkerExecutionRecord, WorkerExecutionRequest
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.origin_access import CacheOriginCredentialRequest, CacheOriginCredentials
from worker.repository_client import (
    RemoteWorkerCredentialService,
    WorkerRepositoryHttpClient,
)
from worker.repository_payloads import (
    AddWorkerRequest,
    GetContainerCredentialsResponse,
    GetNextContainerRequestRequest,
    GetNextContainerRequestResponse,
    WorkerCacheSession,
    WorkerRecordResponse,
)
from worker.tools import (
    ContainerCredentialRequest,
    ContainerCredentials,
)

_CAPACITY_OWNER_ID = "11111111-1111-4111-8111-111111111111"


def test_idle_worker_request_response_returns_control_to_maintenance() -> None:
    transport = _FakeWorkerRepositoryTransport(
        streams={
            "/worker-repository/get-next-container-request": [
                GetNextContainerRequestResponse().model_dump(mode="json"),
                GetNextContainerRequestResponse(
                    container_request=WorkerExecutionRequest(
                        workspace_id="workspace-1",
                        stub_id="stub-1",
                        container_id="next-container",
                    )
                ).model_dump(mode="json"),
            ]
        }
    )
    response = WorkerRepositoryHttpClient(transport).get_next_container_request(
        GetNextContainerRequestRequest(
            worker_id="worker-1",
            cache_generation_id="13f4ff1a-2562-47e7-9f08-964588090ee0",
            cache_session_fence=1,
        )
    )
    assert response.container_request is None


def test_worker_repository_client_preserves_session_auth_and_scoped_credentials() -> None:
    worker = WorkerExecutionRecord(
        worker_id="worker-1",
        pool=MachinePool("default"),
        capacity_owner_id=_CAPACITY_OWNER_ID,
    )
    transport = _FakeWorkerRepositoryTransport(
        posts={
            "/worker-repository/add-worker": WorkerRecordResponse(
                worker=worker,
                worker_session_token="worker-session-token",
                cache_session=WorkerCacheSession(
                    generation_id="13f4ff1a-2562-47e7-9f08-964588090ee0",
                    session_fence=1,
                ),
            ).model_dump(mode="json"),
            "/worker-repository/get-container-credentials": (
                GetContainerCredentialsResponse(
                    credentials=ContainerCredentials(env=["TOKEN=value"])
                ).model_dump(mode="json")
            ),
            "/worker-repository/get-cache-origin-credentials": {
                "credentials": CacheOriginCredentials(
                    image_archive_url="https://signed/image-1.rclip",
                    archive_size_bytes=7,
                    archive_sha256="a" * 64,
                    registry_repository="registry.example.com/workloads",
                    registry_ref="registry.example.com/workloads@sha256:" + "b" * 64,
                    manifest_digest="sha256:" + "b" * 64,
                    architecture="amd64",
                    format_version=2,
                ).model_dump(mode="json"),
            },
        }
    )
    client = WorkerRepositoryHttpClient(transport)

    response = client.add_worker(
        AddWorkerRequest(
            worker=worker,
            cache_generation_id="13f4ff1a-2562-47e7-9f08-964588090ee0",
            cache_storage_id="machine:test-machine",
        )
    )
    credentials = RemoteWorkerCredentialService(client).vend(
        ContainerCredentialRequest(
            workspace_id="workspace-1",
            stub_id="stub-1",
            container_id="ctr-1",
        ),
        principal=WorkerCredentialPrincipal(workspace_id="workspace-1"),
    )
    origin = client.get_cache_origin_credentials(
        CacheOriginCredentialRequest(
            workspace_id="workspace-1",
            stub_id="stub-1",
            container_id="ctr-1",
        )
    ).credentials

    assert response.worker == worker
    assert transport.bearer_token == "worker-session-token"
    assert credentials.env == ["TOKEN=value"]
    assert origin is not None
    assert origin.image_archive_url == "https://signed/image-1.rclip"
    assert origin.archive_size_bytes == 7
    assert origin.archive_sha256 == "a" * 64


class _FakeWorkerRepositoryTransport:
    def __init__(
        self,
        *,
        posts: dict[str, dict[str, JsonValue]] | None = None,
        streams: dict[str, list[dict[str, JsonValue]]] | None = None,
    ) -> None:
        self._posts = posts or {}
        self._streams = streams or {}
        self.posts: list[tuple[str, dict[str, JsonValue]]] = []
        self.streams: list[tuple[str, dict[str, JsonValue]]] = []
        self.bearer_token = "bootstrap-token"

    def set_bearer_token(self, token: str) -> None:
        self.bearer_token = token

    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> dict[str, JsonValue]:
        self.posts.append((path, dict(payload)))
        return self._posts.get(path, {"ok": True})

    def stream(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> Iterator[dict[str, JsonValue]]:
        self.streams.append((path, dict(payload)))
        yield from self._streams.get(path, [])
