from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from container_worker_app.image_archives import BrokeredImageArchiveSourceLoader
from pydantic import JsonValue
from worker.container_startup import WorkerImageSourceLoadRequest
from worker.origin_access import CacheOriginCredentials, ImageRegistryCredentials
from worker.repository_client import WorkerRepositoryHttpClient
from worker.repository_payloads import GetCacheOriginCredentialsResponse


@contextmanager
def _serve_directory(root: Path) -> Iterator[str]:
    handler: Callable[..., SimpleHTTPRequestHandler] = partial(
        SimpleHTTPRequestHandler,
        directory=str(root),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_brokered_image_source_loader_downloads_authorized_index(tmp_path: Path) -> None:
    source = tmp_path / "source.rclip"
    source.write_bytes(b"archive")
    source_sha256 = hashlib.sha256(b"archive").hexdigest()
    target = tmp_path / "cache" / "image-1.rclip"
    with _serve_directory(tmp_path) as source_url:
        transport = _FakeWorkerRepositoryTransport(
            posts={
                "/worker-repository/get-cache-origin-credentials": (
                    GetCacheOriginCredentialsResponse(
                        credentials=CacheOriginCredentials(
                            image_archive_url=f"{source_url}/{source.name}",
                            archive_size_bytes=len(b"archive"),
                            archive_sha256=source_sha256,
                            registry_repository="registry.example.com/workloads",
                            registry_ref=("registry.example.com/workloads@sha256:" + "b" * 64),
                            manifest_digest="sha256:" + "b" * 64,
                            architecture="amd64",
                            format_version=2,
                            registry_credentials=ImageRegistryCredentials(
                                registry="registry.example.com"
                            ),
                        )
                    ).model_dump(mode="json")
                )
            }
        )
        result = BrokeredImageArchiveSourceLoader(
            WorkerRepositoryHttpClient(transport)
        ).load_source_image_archive(
            WorkerImageSourceLoadRequest(
                container_id="ctr-1",
                workspace_id="workspace-1",
                stub_id="stub-1",
                image_id="image-1",
                archive_path=str(target),
                mount_point=str(tmp_path / "mount"),
                cache_path="/images/image-1.rclip",
            )
        )

    assert result.ok
    assert result.bytes_written == len(b"archive")
    assert target.read_bytes() == b"archive"
    assert transport.posts[0][0] == "/worker-repository/get-cache-origin-credentials"
    assert transport.posts[0][1]["workspace_id"] == "workspace-1"
    assert transport.posts[0][1]["container_id"] == "ctr-1"
    assert transport.posts[0][1]["image_id"] == "image-1"


class _FakeWorkerRepositoryTransport:
    def __init__(
        self,
        *,
        posts: Mapping[str, Mapping[str, JsonValue]] | None = None,
    ) -> None:
        self._posts = posts or {}
        self.posts: list[tuple[str, dict[str, JsonValue]]] = []
        self.bearer_token = "bootstrap-token"

    def set_bearer_token(self, token: str) -> None:
        self.bearer_token = token

    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> dict[str, JsonValue]:
        self.posts.append((path, dict(payload)))
        return dict(self._posts.get(path, {"ok": True}))

    def stream(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> Iterator[dict[str, JsonValue]]:
        _ = path, payload
        return iter(())
