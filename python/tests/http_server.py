from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from http.server import HTTPServer
from threading import Thread


@contextmanager
def running_http_server(server: HTTPServer) -> Iterator[HTTPServer]:
    # The stdlib's 500 ms default is also the delay before shutdown notices it.
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        try:
            server.shutdown()
        finally:
            server.server_close()
            thread.join(timeout=2)
            assert not thread.is_alive(), "test HTTP server did not stop"
