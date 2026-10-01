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
import threading
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


write_lock = threading.Lock()


def write_frame(sock, header, payload=b""):
    sys.stdout.flush()
    sys.stderr.flush()
    encoded = json.dumps(header).encode()
    with write_lock:
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
    threaded = load.get("concurrency", 1) > 1
    while True:
        invoke, payload = read_frame(sock)
        if invoke is None:
            return 0
        if threaded:
            # Attempts share the process; each reports its own output as a frame.
            threading.Thread(target=run, args=(sock, handler, invoke, payload, True), daemon=True).start()
        else:
            run(sock, handler, invoke, payload, False)


def run(sock, handler, invoke, payload, threaded):
    call = json.loads(payload)
    if threaded:
        line = f"thread runs {invoke['attempt_id']}\n"
        if call.get("args", [None])[0] == "big":
            line = "x" * (2 << 20) + "\n"
        if call.get("args", [None])[0] == "split":
            # A value split across two frames.
            value = call["args"][1]
            for part in (value[:5], value[5:] + "\n"):
                write_frame(
                    sock,
                    {"type": "output", "attempt_id": invoke["attempt_id"], "stream": "stdout"},
                    part.encode(),
                )
            line = ""
        # Output travels as the payload, at most 256 KiB per frame.
        data = line.encode()
        for start in range(0, len(data), 256 << 10):
            write_frame(
                sock,
                {"type": "output", "attempt_id": invoke["attempt_id"], "stream": "stdout"},
                data[start:start + (256 << 10)],
            )
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
