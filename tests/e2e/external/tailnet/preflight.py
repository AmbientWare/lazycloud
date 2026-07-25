"""Verify an explicitly selected prepared Tailnet node and public gateway."""

from __future__ import annotations

import argparse
import json
import os
import shutil

import httpx
from tests.e2e.external import _support
from tests.e2e.external.tailnet._tailscale import tailscale_status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-node-id", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _support.skip("Tailnet preflight", "--live is required")
    if shutil.which("tailscale") is None:
        return _support.skip("Tailnet preflight", "tailscale CLI is required")
    socket = os.getenv("LAZYCLOUD_E2E_TAILSCALE_SOCKET", "").strip()
    endpoint = os.getenv("LAZYCLOUD_ENDPOINT", "").rstrip("/")
    if not socket or not endpoint:
        return _support.skip(
            "Tailnet preflight",
            "LAZYCLOUD_E2E_TAILSCALE_SOCKET and LAZYCLOUD_ENDPOINT are required",
        )
    try:
        status = tailscale_status(socket)
    except RuntimeError as exc:
        return _support.skip("Tailnet preflight", exc)
    self_node = status.self_node
    if self_node.node_id != args.expected_node_id:
        raise RuntimeError("prepared Tailnet node does not match the guarded target")
    ips = sorted(address for address in (self_node.addresses or ()) if address)
    if not ips or not self_node.online:
        raise RuntimeError("prepared Tailnet node is not online with an assigned address")
    try:
        health = httpx.get(f"{endpoint}/health", timeout=10)
        health.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError("public gateway is unreachable through the prepared transport") from exc
    print(
        json.dumps(
            {
                "accepted": True,
                "node_id": args.expected_node_id,
                "tailnet_ips": ips,
                "gateway_status": health.status_code,
                "cleanup": "read-only; prepared Tailnet deployment retained",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
