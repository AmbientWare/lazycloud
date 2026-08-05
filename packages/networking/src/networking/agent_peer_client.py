"""Ask the agent on this node where a tailnet peer lives.

Resolving a peer requires a tailnet client, and only the agent has one. The
alternative — mounting the tailscaled socket into the worker — would hand a
container running user workloads the ability to take the node off the tailnet,
because that socket grants control rather than lookup.

The worker instead asks over the loopback the agent already listens on, proving
itself with the worker token the agent issued. No new secret is introduced, and
the answer is an address: the worker dials the peer itself, so the agent never
carries the traffic.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

from networking.routing import tailnet_resolve_preface

DEFAULT_AGENT_PEER_TIMEOUT_SECONDS = 10.0
_MAX_REPLY_BYTES = 512


class AgentPeerResolutionError(RuntimeError):
    """The agent could not say where a peer lives."""


@dataclass(frozen=True, slots=True)
class AgentPeerClient:
    """Resolves tailnet peers through the agent's loopback channel."""

    agent_address: str
    worker_token: str
    timeout_seconds: float = DEFAULT_AGENT_PEER_TIMEOUT_SECONDS

    def resolve_peer_host(self, host: str) -> str:
        normalized = host.strip().rstrip(".")
        if not normalized or not self.agent_address or not self.worker_token:
            return ""
        try:
            reply = self._ask(normalized)
        except OSError as exc:
            msg = f"agent peer resolution for {normalized} failed: {exc}"
            raise AgentPeerResolutionError(msg) from exc
        if reply.startswith("ERROR "):
            raise AgentPeerResolutionError(reply.removeprefix("ERROR ").strip())
        return reply

    def wait_for_peer(self, host: str, timeout_seconds: float) -> None:
        """Satisfied by resolution itself.

        The agent waits for the netmap on the worker's behalf, so a separate
        wait here would double the delay for a peer that is simply absent.
        """
        del host, timeout_seconds

    def _ask(self, host: str) -> str:
        target_host, _, target_port = self.agent_address.rpartition(":")
        with socket.create_connection(
            (target_host or "127.0.0.1", int(target_port)),
            timeout=self.timeout_seconds,
        ) as connection:
            connection.sendall(tailnet_resolve_preface(host, self.worker_token))
            connection.settimeout(self.timeout_seconds)
            buffer = b""
            while b"\n" not in buffer and len(buffer) <= _MAX_REPLY_BYTES:
                chunk = connection.recv(min(256, _MAX_REPLY_BYTES - len(buffer)))
                if not chunk:
                    break
                buffer += chunk
        line, _, _ = buffer.partition(b"\n")
        return line.decode("utf-8", errors="replace").strip()


__all__ = [
    "DEFAULT_AGENT_PEER_TIMEOUT_SECONDS",
    "AgentPeerClient",
    "AgentPeerResolutionError",
]
