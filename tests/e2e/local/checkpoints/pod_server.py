"""Stdlib HTTP process whose memory state must survive Pod restoration."""

from __future__ import annotations

import json
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

PORT = 8091
_boot_id = str(uuid4())
_born_at_ns = time.time_ns()
_counter = 0
_lock = threading.Lock()


def _advance() -> None:
    global _counter
    while True:
        time.sleep(0.05)
        with _lock:
            _counter += 1


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path, _, label = self.path.partition("?")
        if path == "/ready":
            self._write(HTTPStatus.OK, {"ready": True})
            return
        if path == "/state":
            with _lock:
                counter = _counter
            self._write(
                HTTPStatus.OK,
                {
                    "label": label,
                    "boot_id": _boot_id,
                    "born_at_ns": _born_at_ns,
                    "counter": counter,
                    "pid": os.getpid(),
                },
            )
            return
        self._write(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, flush=True)

    def _write(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status.value)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    threading.Thread(target=_advance, name="pod-continuity", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
