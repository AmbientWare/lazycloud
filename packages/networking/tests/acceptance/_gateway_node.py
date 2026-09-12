from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
from types import FrameType

from networking.wireguard import derive_wireguard_public_key, generate_wireguard_private_key
from networking.wireguard_client import WireGuardClientRuntime
from networking.wireguard_gateway import WireGuardGatewayRuntime, WireGuardRuntimeService
from pydantic import BaseModel
from shared.http.private_network import WireGuardPeerConfiguration
from tunnel_gateway_app.main import StaticWireGuardPeer, _TcpHealthListener


class GatewayPeer(BaseModel):
    key: str
    address: str


class GatewayConfiguration(BaseModel):
    origin: IPv4Address
    peers: tuple[GatewayPeer, ...]


class Echo(socketserver.BaseRequestHandler):
    request: socket.socket

    def handle(self) -> None:
        while data := self.request.recv(1024):
            self.request.sendall(data)


class Origin(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = self.client_address[0].encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: str) -> None:
        del format, args


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role", choices=("key", "gw0", "gw1", "agent", "platform", "origin", "workload")
    )
    parser.add_argument("key_name", nargs="?", default="")
    options = parser.parse_args()
    role: str = options.role
    root = Path("/proof")
    (root / f"{role}.pid").write_text(str(os.getpid()))
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    if role == "key":
        private_key = generate_wireguard_private_key()
        path = root / options.key_name / "private-key"
        path.parent.mkdir(exist_ok=True)
        path.write_text(private_key)
        path.chmod(0o600)
        print(derive_wireguard_public_key(private_key))
    elif role.startswith("gw"):
        config = GatewayConfiguration.model_validate_json((root / "config.json").read_text())
        runtime = WireGuardGatewayRuntime(
            root / role / "private-key", WireGuardRuntimeService(config.origin, 9000)
        )
        runtime.start(cancelled=stop)
        runtime.reconcile(
            tuple(
                StaticWireGuardPeer(public_key=peer.key, address=peer.address)
                for peer in config.peers
            )
        )
        health = _TcpHealthListener(8080)
        health.start()
        try:
            while not stop.wait(0.25):
                if (root / f"{role}.drain").exists():
                    health.drain()
                (root / f"{role}.status").write_text(
                    json.dumps(
                        {
                            "handshakes": len(runtime.handshakes()),
                            "connections": runtime.active_connections(),
                        }
                    )
                )
        finally:
            health.close()
            runtime.close()
    elif role in ("agent", "platform"):
        configuration = WireGuardPeerConfiguration.model_validate_json(
            (root / f"{role}.json").read_text()
        )
        client = WireGuardClientRuntime(root / role)
        client.configure(configuration)
        try:
            while not stop.wait(0.25):
                (root / f"{role}.status").write_text(json.dumps(client.path_health()))
        finally:
            client.close()
            (root / f"{role}.closed").touch()
    elif role == "workload":
        with socketserver.ThreadingTCPServer(("0.0.0.0", 29443), Echo) as echo:
            echo.daemon_threads = True
            echo.serve_forever()
    elif role == "origin":
        with ThreadingHTTPServer(("0.0.0.0", 9000), Origin) as origin:
            threading.Thread(target=origin.serve_forever, daemon=True).start()
            stop.wait()
            origin.shutdown()


if __name__ == "__main__":
    main()
