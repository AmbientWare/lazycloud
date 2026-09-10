from __future__ import annotations

import hashlib
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree

from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings
from tests.http_server import running_http_server


def test_bulk_deletion_sends_the_checksum_required_by_the_object_store() -> None:
    objects = {"volumes/data/a.txt"}

    class ObjectStore(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            entries = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in objects)
            self.respond(200, f"<ListBucketResult>{entries}</ListBucketResult>".encode())

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            digest = b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()
            if self.headers.get("Content-MD5") != digest:
                self.respond(400, b"<Error><Code>InvalidDigest</Code></Error>")
                return
            for key in ElementTree.fromstring(body).iterfind(".//{*}Key"):
                if key.text:
                    objects.discard(key.text)
            self.respond(200, b"<DeleteResult/>")

        def respond(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ObjectStore)
    with running_http_server(server):
        client = S3ObjectStoreClient.from_settings(
            S3ObjectStoreSettings(
                endpoint_url=f"http://127.0.0.1:{server.server_port}",
                access_key_id="test-access",
                secret_access_key="test-secret",
                bucket="owned-bucket",
            )
        )
        try:
            assert client.delete_prefix("volumes/data/") == ("volumes/data/a.txt",)
            assert objects == set()
        finally:
            client.close()
