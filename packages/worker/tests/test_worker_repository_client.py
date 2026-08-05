from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import suppress

from pydantic import JsonValue
from scheduler.state import (
    SchedulerWorkerRecord,
)
from worker.credential_payloads import WorkerCredentialPrincipal
from worker.origin_access import CacheOriginCredentialRequest, CacheOriginCredentials
from worker.repository_client import (
    RemoteWorkerCredentialService,
    WorkerRepositoryHttpClient,
)
from worker.repository_payloads import (
    AddWorkerRequest,
    GetContainerCredentialsResponse,
    WorkerCacheSession,
    WorkerRecordResponse,
)
from worker.tools import (
    ContainerCredentialRequest,
    ContainerCredentials,
)

_CAPACITY_OWNER_ID = "11111111-1111-4111-8111-111111111111"


def test_worker_repository_client_preserves_session_auth_and_scoped_credentials() -> None:
    worker = SchedulerWorkerRecord(
        worker_id="worker-1",
        pool_name="default",
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
    assert [path for path, _payload in transport.posts] == [
        "/worker-repository/add-worker",
        "/worker-repository/get-container-credentials",
        "/worker-repository/get-cache-origin-credentials",
    ]


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


def test_worker_repository_reaches_a_control_plane_named_by_tailnet_peer() -> None:
    """The worker's busiest hop must follow the destination, not the hostname.

    A remote worker was handed an origin only the control plane could resolve
    and failed there rather than at startup; this is the hop that failed.
    """
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from networking.internal_http import (
        InternalHttpClient,
        TailnetHostPolicy,
        TailnetPeerAddresses,
    )
    from worker.repository_client import build_worker_repository_http_client

    seen: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(self.headers.get("Host", ""))
            body = _json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    class _Peers:
        def wait_for_peer(self, host: str, timeout_seconds: float) -> None:
            del host, timeout_seconds

        def resolve_peer_host(self, host: str) -> str:
            return "127.0.0.1" if host.endswith(".example.ts.net") else ""

        def start(self) -> None: ...

        def close(self) -> None: ...

    def _serve() -> Iterator[int]:
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            server.server_close()

    ports = _serve()
    port = next(ports)
    try:
        http = InternalHttpClient(
            addresses=TailnetPeerAddresses(
                runtime=_Peers(),
                policy=TailnetHostPolicy(dns_suffix="example.ts.net"),
            ),
            timeout_seconds=5.0,
        )
        client = build_worker_repository_http_client(
            endpoint=f"http://control-plane.example.ts.net:{port}",
            token="worker-token",
            http=http,
        )
        assert client.transport.post("/anything", {}) == {"ok": True}
    finally:
        with suppress(StopIteration):
            next(ports)

    # Reached loopback while still addressed to the peer, which is what keeps a
    # signed URL valid and what routes the request over the tailnet.
    assert seen == [f"control-plane.example.ts.net:{port}"]
