from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import sleep

from shared.http_transport import HttpChannel
from tests.http_server import running_http_server
from worker.container_client.models import (
    ContainerCheckpointRequest,
    ContainerCheckpointResponse,
)


def test_checkpoint_requests_can_outlast_connection_timeouts() -> None:
    class CheckpointHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            sleep(0.1)
            body = (
                ContainerCheckpointResponse(ok=True, checkpoint_id="checkpoint-1")
                .model_dump_json()
                .encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CheckpointHandler)
    address = f"127.0.0.1:{server.server_port}"
    with (
        running_http_server(server),
        HttpChannel(endpoint=f"http://{address}", timeout_seconds=0.05) as channel,
    ):
        request = ContainerCheckpointRequest(container_id="container-1")
        response = channel.post("/checkpoint", request.model_dump(mode="json"), timeout_seconds=1.0)
        assert ContainerCheckpointResponse.model_validate(response).checkpoint_id == "checkpoint-1"
