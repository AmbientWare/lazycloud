"""Prove a prepared pool at its maximum rejects one additional workload."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from uuid import uuid4

import httpx
from lazycloud.clients.resource.control import ResourceControlClient

BLOCKED = 77


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-max-replicas", type=int, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _blocked("Kubernetes maximum acceptance requires --live")
    target = _target()
    if target is None:
        return BLOCKED
    context, namespace, release, endpoint, token, workspace = target
    deployment = _worker_deployment(release)
    before = _desired_replicas(context, namespace, deployment)
    if before != args.expected_max_replicas:
        return _blocked(
            f"prepared pool has {before} replicas, expected maximum {args.expected_max_replicas}"
        )
    app_name = f"e2e_kube_maximum_{uuid4().hex[:8]}"
    os.environ["LAZYCLOUD_E2E_KUBERNETES_APP"] = app_name
    from .workloads import maximum_probe

    client = ResourceControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    primary_error: BaseException | None = None
    rejection = ""
    try:
        call = maximum_probe.spawn()
        try:
            call.get(timeout_seconds=60, poll_interval_seconds=0.25)
        except Exception as exc:
            rejection = str(exc)
        else:
            raise RuntimeError("additional workload unexpectedly succeeded at pool maximum")
        lowered = rejection.lower()
        if not any(term in lowered for term in ("capacity", "scheduler", "retry")):
            raise RuntimeError("maximum rejection did not expose a typed capacity failure")
        if _desired_replicas(context, namespace, deployment) != before:
            raise RuntimeError("maximum rejection changed the worker Deployment size")
    except BaseException as exc:
        primary_error = exc
    cleanup_error = _delete_app(client, app_name)
    if primary_error is not None:
        if cleanup_error:
            raise RuntimeError(
                f"maximum enforcement failed and cleanup failed: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error:
        raise RuntimeError(f"maximum enforcement cleanup failed: {cleanup_error}")
    print(
        json.dumps(
            {
                "accepted": True,
                "replicas": before,
                "capacity_rejection": True,
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


def _desired_replicas(context: str, namespace: str, deployment: str) -> int:
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
            "jsonpath={.spec.replicas}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "worker Deployment is unavailable")
    return int(completed.stdout)


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
