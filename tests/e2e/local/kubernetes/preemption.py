"""Verify an operator-triggered worker interruption retained one task's retry identity."""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from lazycloud.clients.resource.control import ResourceControlClient
from shared.container_requests import StopContainerReason
from shared.containers import ContainerStatus

BLOCKED = 77
TERMINAL = {
    ContainerStatus.Exited,
    ContainerStatus.Failed,
    ContainerStatus.Stopped,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--source-container-id", required=True)
    parser.add_argument("--interrupted-worker-id", required=True)
    parser.add_argument("--replacement-worker-id", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _blocked("Kubernetes preemption acceptance requires --live")
    endpoint = os.getenv("LAZYCLOUD_ENDPOINT", "").rstrip("/")
    token = os.getenv("LAZYCLOUD_E2E_ADMIN_TOKEN", "")
    workspace = os.getenv("LAZYCLOUD_WORKSPACE", "default")
    if not endpoint or not token:
        return _blocked("LAZYCLOUD_ENDPOINT and LAZYCLOUD_E2E_ADMIN_TOKEN are required")
    try:
        httpx.get(f"{endpoint}/health", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        return _blocked(f"prepared public endpoint is unavailable: {exc}")
    client = ResourceControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    containers = [item.container for item in client.list_containers(limit=1000).data]
    sources = [
        item
        for item in containers
        if item.id == args.source_container_id and item.task_id == args.task_id
    ]
    if len(sources) != 1:
        raise RuntimeError("source container/task identity is not publicly observable")
    source = sources[0]
    if (
        source.status not in TERMINAL
        or source.termination_reason is not StopContainerReason.Preempted
        or (source.runtime_worker_id or source.worker_id) != args.interrupted_worker_id
    ):
        raise RuntimeError("source container lacks typed preemption evidence")
    retries = [
        item
        for item in containers
        if item.task_id == args.task_id
        and item.id != source.id
        and (item.runtime_worker_id or item.worker_id) == args.replacement_worker_id
        and item.status
        in {
            ContainerStatus.Pending,
            ContainerStatus.Running,
            ContainerStatus.Exited,
        }
    ]
    if len(retries) != 1:
        raise RuntimeError("task did not retain one retry on the replacement worker")
    workers = client.list_workers().workers
    if any(worker.id == args.interrupted_worker_id for worker in workers):
        raise RuntimeError("interrupted worker still appears in the public worker set")
    replacements = [worker for worker in workers if worker.id == args.replacement_worker_id]
    if len(replacements) != 1 or replacements[0].status not in {"ready", "available", "busy"}:
        raise RuntimeError("replacement worker is not publicly ready")
    print(
        json.dumps(
            {
                "accepted": True,
                "task_id": args.task_id,
                "source_container_id": source.id,
                "retry_container_id": retries[0].id,
                "interrupted_worker_id": args.interrupted_worker_id,
                "replacement_worker_id": args.replacement_worker_id,
                "cleanup": "read-only; operator-owned release retained",
            },
            sort_keys=True,
        )
    )
    return 0


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
