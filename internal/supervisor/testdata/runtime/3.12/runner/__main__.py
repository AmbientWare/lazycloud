"""A minimal runner for host runtime tests.

It speaks contracts/runner.yaml with JSON payloads only, so tests exercise the
supervisor and agent against the real protocol without the Python packet's
runner. Inputs are {"args": [...], "kwargs": {...}}.
"""

import importlib
import json
import os
import socket
import struct
import sys
import traceback


def read_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_frame(sock):
    length = read_exact(sock, 4)
    if length is None:
        return None, None
    header = json.loads(read_exact(sock, struct.unpack(">I", length)[0]))
    payload = read_exact(sock, struct.unpack(">I", read_exact(sock, 4))[0])
    return header, payload


def write_frame(sock, header, payload=b""):
    sys.stdout.flush()
    sys.stderr.flush()
    encoded = json.dumps(header).encode()
    sock.sendall(
        struct.pack(">I", len(encoded)) + encoded + struct.pack(">I", len(payload)) + payload
    )


def error(exc):
    return {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}


def main():
    sock = socket.socket(fileno=int(os.environ["LAZYCLOUD_RUNNER_FD"]))
    load, _ = read_frame(sock)
    sys.path.insert(0, os.getcwd())
    try:
        module, qualname = load["handler"].split(":")
        handler = importlib.import_module(module)
        for part in qualname.split("."):
            handler = getattr(handler, part)
    except Exception as exc:
        write_frame(sock, {"type": "load_failed", "error": error(exc)})
        return 1
    write_frame(sock, {"type": "loaded"})
    while True:
        invoke, payload = read_frame(sock)
        if invoke is None:
            return 0
        call = json.loads(payload)
        try:
            result = handler(*call.get("args", []), **call.get("kwargs", {}))
        except Exception as exc:
            write_frame(
                sock, {"type": "failed", "attempt_id": invoke["attempt_id"], "error": error(exc)}
            )
        else:
            write_frame(
                sock,
                {"type": "succeeded", "attempt_id": invoke["attempt_id"], "result_encoding": "json"},
                json.dumps(result).encode(),
            )


sys.exit(main())
