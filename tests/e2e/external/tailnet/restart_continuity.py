"""Verify a separately restarted Tailnet agent retained identity and routing."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

import httpx
from lazycloud.clients.resource.control import ResourceControlClient
from shared.http.compute import WorkerResponse
from tests.e2e.external import _support
from tests.e2e.external.tailnet._tailscale import tailscale_status


def _wait_post_restart_heartbeat(
    client: ResourceControlClient,
    worker_id: str,
    machine_id: str,
    restarted_after: datetime,
    deadline: _support.Deadline,
) -> WorkerResponse:
    def check() -> WorkerResponse | None:
        workers = [worker for worker in client.list_workers().workers if worker.id == worker_id]
        if len(workers) != 1:
            raise RuntimeError("restarted worker identity was not retained")
        worker = workers[0]
        if worker.machine_id != machine_id:
            raise RuntimeError("restarted worker moved to a different machine")
        if worker.updated_at <= restarted_after:
            return None
        return worker

    return _support.poll_until(deadline, "a post-restart worker heartbeat", check)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--expected-node-id", required=True)
    parser.add_argument("--invoke-url", required=True)
    parser.add_argument("--restarted-after", type=datetime.fromisoformat, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _support.skip("Tailnet restart", "--live is required")
    if args.restarted_after.tzinfo is None:
        raise RuntimeError("--restarted-after must include a timezone")
    invocation_token = os.getenv("LAZYCLOUD_TOKEN", "")
    socket = os.getenv("LAZYCLOUD_E2E_TAILSCALE_SOCKET", "")
    try:
        endpoint, token, workspace = _support.prepared_gateway(
            token_variable="LAZYCLOUD_E2E_ADMIN_TOKEN"
        )
        if not invocation_token or not socket:
            raise _support.MissingPrerequisite(
                "LAZYCLOUD_TOKEN and the prepared Tailscale socket are required"
            )
    except _support.MissingPrerequisite as exc:
        return _support.skip("Tailnet restart", exc)
    deadline = _support.Deadline(args.timeout)
    client = ResourceControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    worker = _wait_post_restart_heartbeat(
        client,
        args.worker_id,
        args.machine_id,
        args.restarted_after,
        deadline,
    )
    status = tailscale_status(socket)
    if status.self_node.node_id != args.expected_node_id:
        raise RuntimeError("Tailnet node identity changed across restart")
    response = httpx.post(
        args.invoke_url,
        json={"args": ["post-restart"], "kwargs": {}},
        headers={"Authorization": f"Bearer {invocation_token}"},
        timeout=120,
    )
    response.raise_for_status()
    if response.json() != {"message": "post-restart"}:
        raise RuntimeError("post-restart Endpoint returned an unexpected response")
    print(
        json.dumps(
            {
                "accepted": True,
                "worker_id": worker.id,
                "machine_id": worker.machine_id,
                "node_id": args.expected_node_id,
                "worker_updated_at": worker.updated_at.isoformat(),
                "post_restart_task_id": response.headers.get("x-task-id", ""),
                "cleanup": "read-only; prepared agent and Endpoint retained",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
