"""Prove one public workload causes one prepared worker pool to scale up."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from lazycloud.clients.resource.control import ResourceControlClient

BLOCKED = 77


def main() -> int:
    if "--live" not in sys.argv:
        return _blocked("Kubernetes scale-up acceptance requires --live")
    target = _target()
    if target is None:
        return BLOCKED
    context, namespace, release, endpoint, token, workspace = target
    app_name = f"e2e_kube_scale_{uuid4().hex[:8]}"
    os.environ["LAZYCLOUD_E2E_KUBERNETES_APP"] = app_name
    from .workloads import scale_probe

    client = ResourceControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    deployment = _worker_deployment(release)
    baseline = _replicas(context, namespace, deployment)
    if baseline["desired"] != baseline["ready"]:
        return _blocked("prepared worker pool is not ready at its baseline")
    call = None
    primary_error: BaseException | None = None
    evidence: dict[str, object] = {}
    try:
        call = scale_probe.spawn(30)
        scaled = _await_replica_increase(
            context,
            namespace,
            deployment,
            baseline["desired"],
        )
        result = call.get(timeout_seconds=180, poll_interval_seconds=0.25)
        if result != {"delay_seconds": 30}:
            raise RuntimeError("scale-up workload returned an unexpected result")
        evidence = {
            "task_id": call.task_id,
            "baseline_replicas": baseline["desired"],
            "scaled_replicas": scaled["desired"],
            "ready_replicas": scaled["ready"],
        }
    except BaseException as exc:
        primary_error = exc
        if call is not None:
            with contextlib.suppress(Exception):
                call.task.cancel()
    cleanup_error = _delete_app(client, app_name)
    if primary_error is not None:
        if cleanup_error:
            raise RuntimeError(
                f"Kubernetes scale-up failed and cleanup failed: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error:
        raise RuntimeError(f"Kubernetes scale-up cleanup failed: {cleanup_error}")
    print(
        json.dumps(
            {
                "accepted": True,
                "evidence": evidence,
                "cleanup": "owned app deleted; prepared release retained",
            },
            sort_keys=True,
        )
    )
    return 0


def _target() -> tuple[str, str, str, str, str, str] | None:
    context = os.getenv("LAZYCLOUD_E2E_KUBERNETES_CONTEXT", "").strip()
    namespace = os.getenv("LAZYCLOUD_E2E_KUBERNETES_NAMESPACE", "").strip()
    release = os.getenv("LAZYCLOUD_E2E_KUBERNETES_RELEASE", "").strip()
    endpoint = os.getenv("LAZYCLOUD_ENDPOINT", "").strip()
    token = os.getenv("LAZYCLOUD_TOKEN", "").strip()
    if not all((context, namespace, release, endpoint, token)):
        _blocked("explicit prepared-cluster target and public endpoint/token are required")
        return None
    try:
        httpx.get(f"{endpoint.rstrip('/')}/health", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        _blocked(f"prepared public endpoint is unavailable: {exc}")
        return None
    return (
        context,
        namespace,
        release,
        endpoint,
        token,
        os.getenv("LAZYCLOUD_WORKSPACE", "default"),
    )


def _worker_deployment(release: str) -> str:
    fullname = release if "lazycloud" in release else f"{release}-lazycloud"
    return f"{fullname[:63].rstrip('-')}-container-worker-default"


def _replicas(context: str, namespace: str, deployment: str) -> dict[str, int]:
    completed = subprocess.run(
        [
            "kubectl",
            "--context",
            context,
            "--namespace",
            namespace,
            "get",
            "deployment",
            deployment,
            "-o",
            "json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "worker Deployment is unavailable")
    value = json.loads(completed.stdout)
    return {
        "desired": int(value.get("spec", {}).get("replicas", 0)),
        "ready": int(value.get("status", {}).get("readyReplicas", 0)),
    }


def _await_replica_increase(
    context: str,
    namespace: str,
    deployment: str,
    baseline: int,
) -> dict[str, int]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = _replicas(context, namespace, deployment)
        if current["desired"] > baseline and current["ready"] == current["desired"]:
            return current
        time.sleep(1)
    raise RuntimeError("prepared Kubernetes worker pool did not scale up")


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
        return str(exc)
    return ""


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
