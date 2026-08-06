"""Prove one unique Endpoint executes on an explicitly selected Tailnet worker."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from lazycloud.clients.resource.control import ResourceControlClient
from shared.http.compute import ContainerResponse
from tests.e2e.external import _support

SOURCE_ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-name", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _support.skip("Tailnet endpoint", "--live is required")
    try:
        endpoint, token, workspace = _support.prepared_gateway()
    except _support.MissingPrerequisite as exc:
        return _support.skip("Tailnet endpoint", exc)
    deadline = _support.Deadline(args.timeout)
    app_name = f"e2e_tailnet_route_{uuid4().hex[:8]}"
    os.environ["LAZYCLOUD_E2E_TAILNET_APP"] = app_name
    os.environ["LAZYCLOUD_E2E_TAILNET_POOL"] = args.pool
    from tests.e2e.external.tailnet.workloads import tailnet_route

    client = ResourceControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    primary_error: BaseException | None = None
    evidence: dict[str, object] = {}
    try:
        deployed = tailnet_route.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        response = tailnet_route.target("deployed").request("tailnet-ready")
        if response.status_code != 200 or response.json() != {"message": "tailnet-ready"}:
            raise RuntimeError("Tailnet Endpoint returned an unexpected public response")
        task_ids = response.headers.get("X-Task-Id") or response.headers.get("x-task-id") or []
        task_id = task_ids[0] if task_ids else ""
        if not task_id:
            raise RuntimeError("Tailnet Endpoint response omitted its task identity")
        container = _await_task_container(client, task_id, deadline)
        assigned_worker = container.runtime_worker_id or container.worker_id
        if assigned_worker != args.worker_id:
            raise RuntimeError("Tailnet Endpoint executed on the wrong worker")
        evidence = {
            "app_name": app_name,
            "deployment_id": deployed.deployment_id,
            "task_id": task_id,
            "container_id": container.id,
            "worker_id": assigned_worker,
        }
    except BaseException as exc:
        primary_error = exc
        print(f"recovery: Tailnet endpoint app {app_name}", file=sys.stderr)
    cleanup_error = _delete_app(client, app_name)
    if primary_error is not None:
        if cleanup_error:
            raise RuntimeError(
                f"Tailnet Endpoint app {app_name} failed and cleanup failed: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error:
        raise RuntimeError(f"Tailnet Endpoint app {app_name} cleanup failed: {cleanup_error}")
    print(
        json.dumps(
            {
                "accepted": True,
                "evidence": evidence,
                "cleanup": "owned app deleted; prepared Tailnet deployment retained",
            },
            sort_keys=True,
        )
    )
    return 0


def _await_task_container(
    client: ResourceControlClient,
    task_id: str,
    deadline: _support.Deadline,
) -> ContainerResponse:
    def check() -> ContainerResponse | None:
        matches = [
            item.container
            for item in client.list_containers(limit=1000).data
            if item.container.task_id == task_id
        ]
        if len(matches) > 1:
            raise RuntimeError("Tailnet task produced multiple containers")
        return matches[0] if matches else None

    return _support.poll_until(
        deadline,
        "the Tailnet task container to become publicly observable",
        check,
        interval_seconds=0.5,
    )


def _delete_app(client: ResourceControlClient, app_name: str) -> str:
    try:
        matches = [item for item in client.list_apps().data if item.name == app_name]
        if len(matches) > 1:
            return f"multiple apps matched unique name {app_name}"
        if matches:
            client.delete_app(matches[0].id)
        if any(item.name == app_name for item in client.list_apps().data):
            return f"app {app_name} remains after public deletion"
    except Exception as exc:
        return f"app {app_name}: {exc}"
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
