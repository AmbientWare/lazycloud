from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from lazycloud.http_transport import request_raw
from lazycloud.session.uploads import stream_object_bytes, stream_object_file
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.http.errors import HttpApiError, HttpResponseDecodeError


class _TransportHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args

    def _handle(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/final?redirected=1")
            self.end_headers()
            return
        if parsed.path == "/slow":
            time.sleep(0.1)
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path == "/malformed":
            self._respond(200, b"not-json", content_type="application/json")
            return
        if parsed.path == "/empty-object":
            self._respond(200, b'{"task_id": []}', content_type="application/json")
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


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


def test_object_upload_streams_with_progress_and_validates_response() -> None:
    data = b"streamed source"
    completed: list[int] = []

    with _http_server() as endpoint:
        response = stream_object_bytes(
            endpoint=endpoint,
            token="test-token",
            workspace="tenant-a",
            data=data,
            name="source.tar.gz",
            object_hash=hashlib.sha256(data).hexdigest(),
            bucket=SOURCE_PACKAGE_BUCKET,
            overwrite=False,
            content_type="application/gzip",
            metadata={"kind": "source"},
            timeout_seconds=2.0,
            progress=completed.append,
            chunk_size=3,
        )

    assert response.object_id == "obj-stream"
    assert completed == [3, 6, 9, 12, len(data)]


def test_object_file_upload_streams_without_loading_a_second_copy(
    tmp_path: Path,
) -> None:
    data = b"streamed file source"
    source = tmp_path / "source.tar.gz"
    source.write_bytes(data)
    completed: list[int] = []

    with _http_server() as endpoint:
        response = stream_object_file(
            endpoint=endpoint,
            token="test-token",
            workspace="tenant-a",
            source=source,
            size=len(data),
            name=source.name,
            object_hash=hashlib.sha256(data).hexdigest(),
            bucket=SOURCE_PACKAGE_BUCKET,
            overwrite=False,
            content_type="application/gzip",
            metadata={"kind": "source"},
            timeout_seconds=2.0,
            progress=completed.append,
            chunk_size=4,
        )

    assert response.object_id == "obj-stream"
    assert completed == [4, 8, 12, 16, len(data)]


def test_object_upload_maps_http_failures_and_rejects_invalid_success() -> None:
    data = b"source"
    digest = hashlib.sha256(data).hexdigest()

    with _http_server() as endpoint:
        with pytest.raises(HttpApiError, match="workspace is deleting") as conflict:
            stream_object_bytes(
                endpoint=endpoint,
                token="test-token",
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
                endpoint=endpoint,
                token="test-token",
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
