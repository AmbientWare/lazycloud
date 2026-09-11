from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from lazycloud.cli.main import build_public_cli
from lazycloud.http_transport import request_raw
from lazycloud.session import Client
from lazycloud.session.deployment import DeploymentClient
from lazycloud.session.uploads import stream_object_bytes
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.client_version import RECOMMENDED_CLIENT_VERSION_HEADER, observe_client_versions
from shared.deployment_records import DeploymentSpec
from shared.http.errors import HttpApiError, HttpResponseDecodeError
from shared.http_transport import HttpChannel
from tests.http_server import running_http_server
from typer.testing import CliRunner


class _TransportHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args

    def _handle(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/v1/workspaces":
            self._respond(200, b'{"workspaces": []}', content_type="application/json")
            return
        if parsed.path == "/api/v1/workspaces/current":
            self._respond(
                200,
                b'{"id":"workspace-1","name":"default",'
                b'"created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}',
                content_type="application/json",
            )
            return
        if parsed.path == "/api/v1/functions/invoke/stream":
            self._respond(200, b'{"task_id": []}\n', content_type="application/x-ndjson")
            return
        if parsed.path.startswith("/status/"):
            status = int(parsed.path.rsplit("/", maxsplit=1)[-1])
            body = json.dumps({"detail": f"status {status}"}).encode()
            self._respond(status, body, content_type="application/json", duplicate_header=True)
            return

        if parsed.path == "/gateway/objects/stream":
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("name") == ["denied"]:
                self._respond(
                    409,
                    b'{"detail":"workspace is deleting"}',
                    content_type="application/json",
                )
                return
            if query.get("name") == ["invalid-response"]:
                self._respond(200, b"{}", content_type="application/json")
                return
            assert query["hash"] == [hashlib.sha256(body).hexdigest()]
            assert query["size"] == [str(len(body))]
            assert query["workspace"] == ["tenant-a"]
            assert query["bucket"] == [SOURCE_PACKAGE_BUCKET]
            assert self.headers.get("Authorization") == "Bearer test-token"
            assert self.headers.get("X-Object-Meta-kind") == "source"
            self._respond(200, b'{"object_id":"obj-stream"}', content_type="application/json")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        payload = json.dumps(
            {
                "path": parsed.path,
                "query": urllib.parse.parse_qs(parsed.query),
                "body": body.decode(),
                "content_type": self.headers.get("Content-Type", ""),
                "authorization": self.headers.get("Authorization", ""),
            }
        ).encode()
        self._respond(200, payload, content_type="application/json")

    def _respond(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str,
        duplicate_header: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header(RECOMMENDED_CLIENT_VERSION_HEADER, "999.0.0")
        if duplicate_header:
            self.send_header("X-Request-Result", "one")
            self.send_header("X-Request-Result", "two")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _http_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TransportHandler)
    server.daemon_threads = True
    with running_http_server(server):
        yield f"http://127.0.0.1:{server.server_port}"


def test_raw_transport_sends_a_bounded_readable_body_without_json_encoding() -> None:
    reads: list[int] = []

    class Body:
        def __init__(self) -> None:
            self.offset = 0

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            if self.offset >= len(b"streamed"):
                return b""
            selected = 2 if size < 0 else min(size, 2)
            chunk = b"streamed"[self.offset : self.offset + selected]
            self.offset += len(chunk)
            return chunk

    with _http_server() as endpoint:
        response = request_raw(
            f"{endpoint}/echo",
            method="POST",
            data=Body(),
            headers={"Content-Length": str(len(b"streamed"))},
        )

    assert json.loads(response.content)["body"] == "streamed"
    assert len(reads) > 1


def test_cli_version_advice_keeps_json_stdout_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    with _http_server() as endpoint:
        monkeypatch.setenv("LAZYCLOUD_ENDPOINT", endpoint)
        result = CliRunner().invoke(build_public_cli(), ["--json", "workspace", "list"])
        human_result = CliRunner().invoke(build_public_cli(), ["workspace", "list"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"workspaces": []}
    assert "999.0.0" in result.stderr
    assert "lazycloud update" in result.stderr
    assert human_result.exit_code == 0, human_result.output
    assert human_result.stderr.count("lazycloud update") == 1


def test_version_advice_reaches_the_client_on_streams_and_http_errors() -> None:
    versions: list[str] = []
    with _http_server() as endpoint, observe_client_versions(versions.append):
        channel = HttpChannel(endpoint=endpoint)
        list(channel.stream_get("/echo"))
        list(channel.stream_post("/api/v1/functions/invoke/stream", {}))
        with pytest.raises(HttpApiError):
            channel.get("/status/403")
        response = request_raw(endpoint, method="GET", path="/status/403")
        assert response.status_code == 403
    assert versions == ["999.0.0"] * 4


def test_object_upload_streams_with_progress_and_validates_response() -> None:
    data = b"streamed source"
    completed: list[int] = []

    with _http_server() as endpoint:
        response = (
            Client(workspace="tenant-a")
            ._bind_control(endpoint=endpoint, token="test-token")
            .upload_bytes(
                data,
                name="source.tar.gz",
                bucket=SOURCE_PACKAGE_BUCKET,
                overwrite=False,
                content_type="application/gzip",
                metadata={"kind": "source"},
                progress=completed.append,
            )
        )

    assert response.object_id == "obj-stream"
    assert response.size == len(data)
    assert response.sha256 == hashlib.sha256(data).hexdigest()
    assert completed[-1] == len(data)


def test_object_file_upload_streams_without_loading_a_second_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"streamed file source" * 100_000
    source = tmp_path / "source.tar.gz"
    source.write_bytes(data)
    completed: list[int] = []

    def reject_read_bytes(path: Path) -> bytes:
        pytest.fail(f"upload must stream {path.name}")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    with _http_server() as endpoint:
        response = (
            Client(workspace="tenant-a")
            ._bind_control(endpoint=endpoint, token="test-token")
            .upload_file(
                source,
                name=source.name,
                bucket=SOURCE_PACKAGE_BUCKET,
                overwrite=False,
                content_type="application/gzip",
                metadata={"kind": "source"},
                progress=completed.append,
            )
        )

    assert response.object_id == "obj-stream"
    assert response.size == len(data)
    assert response.sha256 == hashlib.sha256(data).hexdigest()
    assert len(completed) > 1
    assert completed == sorted(completed)
    assert completed[-1] == len(data)


def test_object_upload_maps_http_failures_and_rejects_invalid_success() -> None:
    data = b"source"
    digest = hashlib.sha256(data).hexdigest()

    with _http_server() as endpoint:
        with pytest.raises(HttpApiError, match="workspace is deleting") as conflict:
            stream_object_bytes(
                channel=HttpChannel(endpoint=endpoint, token="test-token"),
                workspace="tenant-a",
                data=data,
                name="denied",
                object_hash=digest,
                bucket=SOURCE_PACKAGE_BUCKET,
                overwrite=False,
                content_type="application/gzip",
                metadata=None,
                timeout_seconds=2.0,
                progress=None,
            )
        with pytest.raises(HttpResponseDecodeError, match="invalid JSON"):
            stream_object_bytes(
                channel=HttpChannel(endpoint=endpoint, token="test-token"),
                workspace="tenant-a",
                data=data,
                name="invalid-response",
                object_hash=digest,
                bucket=SOURCE_PACKAGE_BUCKET,
                overwrite=False,
                content_type="application/gzip",
                metadata=None,
                timeout_seconds=2.0,
                progress=None,
            )

    assert conflict.value.status_code == 409


def test_preparation_checks_source_existence_in_each_workspace(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def hello(): return 'hello'\n")
    stored: dict[tuple[str, str], str] = {}
    uploads: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            workspace = query["workspace"][0]
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            assert self.headers["Authorization"] == "Bearer test-token"
            response: dict[str, object]
            if parsed.path == "/gateway/objects/stream":
                digest = hashlib.sha256(body).hexdigest()
                assert query["hash"] == [digest]
                object_id = f"{workspace}-object-{len(uploads)}"
                stored[workspace, digest] = object_id
                uploads.append(workspace)
                response = {"object_id": object_id}
            else:
                payload = json.loads(body)
                if parsed.path == "/api/v1/images/verify-build":
                    response = {"image_id": "image-cached", "valid": True, "exists": True}
                elif parsed.path == "/gateway/objects/head":
                    object_id = stored.get((workspace, payload["hash"]), "")
                    response = {"exists": bool(object_id), "object_id": object_id}
                elif parsed.path == "/gateway/stubs/get-or-create":
                    assert payload["object_id"] in {
                        value for (owner, _), value in stored.items() if owner == workspace
                    }
                    response = {"stub_id": "stub-prepared"}
                else:
                    raise AssertionError(parsed.path)
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    with running_http_server(server):
        for index, workspace in enumerate(("tenant-a", "tenant-a", "tenant-b", "tenant-a")):
            if index == 3:
                stored.clear()
            deployment = DeploymentClient(
                endpoint=f"http://127.0.0.1:{server.server_port}",
                token="test-token",
                workspace=workspace,
                sync_source=True,
                source_root=tmp_path,
            )
            response = deployment.prepare(DeploymentSpec(name="hello"))
            assert response.stub_id == "stub-prepared"
    assert uploads == ["tenant-a", "tenant-b", "tenant-a"]
