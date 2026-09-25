from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import Future
from pathlib import Path
from typing import IO

import httpx
import pytest
from networking.internal_http import InternalHttpClient, InternalHttpConnectError
from pydantic import JsonValue
from shared.http.errors import HttpApiError
from shared.placement import Placement
from shared.scheduling import WorkerExecutionRecord, WorkerExecutionRequest
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.origin_access import CacheOriginCredentialRequest, CacheOriginCredentials
from worker.repository_client import (
    RemoteWorkerCredentialService,
    WorkerRepositoryHttpClient,
    WorkerRepositoryHttpTransport,
)
from worker.repository_errors import WorkerRepositoryClientError
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

from worker import repository_client

_CAPACITY_OWNER_ID = "11111111-1111-4111-8111-111111111111"


def test_idle_worker_request_response_returns_control_to_maintenance() -> None:
    transport = _FakeWorkerRepositoryTransport(
        streams={
            "/worker-repository/get-next-container-request": [
                GetNextContainerRequestResponse().model_dump(mode="json"),
                GetNextContainerRequestResponse(
                    container_request=WorkerExecutionRequest(
                        placement=Placement.platform(),
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
        placement=Placement.platform(),
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


class _RefusedHttp(InternalHttpClient):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | IO[bytes] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        raise InternalHttpConnectError(f"{method} refused")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize(
    ("hold_seconds", "gives_up_at", "error"),
    [(0.0, 60.0, "refused"), (1800.0, 1800.0, "did not admit this reserve worker")],
)
def test_a_refused_worker_waits_longer_only_when_the_agent_holds_it(
    monkeypatch: pytest.MonkeyPatch, hold_seconds: float, gives_up_at: float, error: str
) -> None:
    """A worker gives up on a refused agent after its bound; a held reserve's is the hold."""
    clock = _Clock()
    monkeypatch.setattr(repository_client, "time", clock)
    transport = WorkerRepositoryHttpTransport(
        endpoint="http://agent.invalid",
        token="worker-secret",
        admission_hold_seconds=hold_seconds,
        http=_RefusedHttp(),
    )

    with pytest.raises(WorkerRepositoryClientError, match=error):
        transport.post("/worker-repository/add-worker", {})

    assert gives_up_at - 1 <= clock.now <= gives_up_at


class _FencedThenRefusedHttp(InternalHttpClient):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | IO[bytes] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        if "add-worker" in url:
            return httpx.Response(409, json={"detail": "machine has not been authorized"})
        raise InternalHttpConnectError(f"{method} refused")


def test_a_fenced_worker_stays_held_after_the_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fence's 409 does not admit a held worker, so it keeps waiting and its marker stays."""
    clock = _Clock()
    monkeypatch.setattr(repository_client, "time", clock)
    marker = tmp_path / "admission-waiting"
    marker.touch()
    transport = WorkerRepositoryHttpTransport(
        endpoint="http://agent.invalid",
        token="worker-secret",
        admission_hold_seconds=1800.0,
        admission_waiting_file=marker,
        http=_FencedThenRefusedHttp(),
    )

    with pytest.raises(HttpApiError):
        transport.post("/worker-repository/add-worker", {})
    with pytest.raises(WorkerRepositoryClientError, match="did not admit this reserve worker"):
        transport.post("/worker-repository/keepalive", {})

    assert clock.now >= 1799
    assert marker.exists()


@pytest.mark.parametrize(
    ("outcome", "error", "waited", "reported"),
    [
        ("running", "did not admit this reserve worker", 5.0, False),
        ("prepared", "did not admit this reserve worker", 5.0, True),
        ("failed", "readiness preparation failed during the admission hold", 0.0, False),
    ],
)
def test_a_held_worker_waits_on_its_readiness_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
    error: str,
    waited: float,
    reported: bool,
) -> None:
    """A held worker says it waits only once prepared, and a failed preparation ends the hold.

    A reserve hibernates once its worker waits, and one whose preparation failed
    would otherwise hold until the reserve timed out, hiding the cause.
    """
    clock = _Clock()
    monkeypatch.setattr(repository_client, "time", clock)
    marker = tmp_path / "admission-waiting"
    readiness: Future[None] = Future()
    if outcome == "prepared":
        readiness.set_result(None)
    elif outcome == "failed":
        readiness.set_exception(RuntimeError("managed runtime catalog is unavailable"))
    transport = WorkerRepositoryHttpTransport(
        endpoint="http://agent.invalid",
        token="worker-secret",
        admission_hold_seconds=5.0,
        admission_waiting_file=marker,
        admission_readiness=readiness,
        http=_RefusedHttp(),
    )

    with pytest.raises(WorkerRepositoryClientError, match=error):
        transport.post("/worker-repository/add-worker", {})

    if waited:
        assert waited - 1 <= clock.now <= waited
    else:
        assert clock.now == 0
    assert marker.exists() is reported
