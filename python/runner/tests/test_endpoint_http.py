from __future__ import annotations

import json
from http.client import HTTPConnection

import pytest
from runner.serve import EndpointServeRunner
from shared.container_requests import CONTAINER_HEALTH_PATH
from shared.http.endpoints import EndpointForwardResponse

from tests.http_server import running_http_server


def test_endpoint_persistent_connection_frames_responses_and_consumes_bodies() -> None:
    def handler(status: int = 200) -> EndpointForwardResponse:
        return EndpointForwardResponse(
            status_code=status,
            body=b"result",
            headers={"content-length": ["999"], "transfer-encoding": ["chunked"]},
        )

    runner = EndpointServeRunner(handler_ref="", host="127.0.0.1", port=0)
    runner._handler = handler
    with running_http_server(runner.create_server()) as server:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.connect()
        original_socket = connection.sock
        try:
            for method, path, body, status, expected in (
                ("POST", CONTAINER_HEALTH_PATH, b"discard", 200, b"ok"),
                ("POST", "/", b"{}", 200, b"result"),
                ("HEAD", "/", b"{}", 200, b""),
                ("POST", "/", json.dumps({"status": 204}).encode(), 204, b""),
                ("POST", "/", json.dumps({"status": 205}).encode(), 205, b""),
                ("POST", "/", json.dumps({"status": 304}).encode(), 304, b""),
                ("POST", "/", b"{}", 200, b"result"),
            ):
                connection.request(method, path, body=body)
                response = connection.getresponse()
                assert response.status == status
                assert response.read() == expected
                assert response.version == 11
                assert response.getheader("transfer-encoding") is None
                assert connection.sock is original_socket
        finally:
            connection.close()


@pytest.mark.parametrize("headers", [{"Content-Length": "-1"}, {"Transfer-Encoding": "chunked"}])
def test_endpoint_rejects_ambiguous_request_framing_and_closes_connection(
    headers: dict[str, str],
) -> None:
    runner = EndpointServeRunner(handler_ref="", host="127.0.0.1", port=0)
    with running_http_server(runner.create_server()) as server:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("POST", CONTAINER_HEALTH_PATH, body=b"", headers=headers)
            response = connection.getresponse()
            assert response.status == 400
            assert response.getheader("connection") == "close"
            response.read()
            assert connection.sock is None
        finally:
            connection.close()
