"""Verify an idle prepared worker pool returns to its declared minimum."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

BLOCKED = 77


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-min-replicas", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _blocked("Kubernetes scale-down acceptance requires --live")
    target = _target()
    if target is None:
        return BLOCKED
    context, namespace, release = target
    deployment = _worker_deployment(release)
    deadline = time.monotonic() + args.timeout_seconds
    latest: dict[str, int] = {}
    while time.monotonic() < deadline:
        latest = _replicas(context, namespace, deployment)
        if (
            latest["desired"] == args.expected_min_replicas
            and latest["ready"] == args.expected_min_replicas
        ):
            print(
                json.dumps(
                    {
                        "accepted": True,
                        "deployment": deployment,
                        "minimum_replicas": args.expected_min_replicas,
                        "cleanup": "read-only; operator-owned release retained",
                    },
                    sort_keys=True,
                )
            )
            return 0
        if latest["desired"] < args.expected_min_replicas:
            raise RuntimeError("worker pool scaled below its declared minimum")
        time.sleep(2)
    raise RuntimeError(f"worker pool did not return to its declared minimum: {latest}")


def _target() -> tuple[str, str, str] | None:
    context = os.getenv("LAZYCLOUD_E2E_KUBERNETES_CONTEXT", "").strip()
    namespace = os.getenv("LAZYCLOUD_E2E_KUBERNETES_NAMESPACE", "").strip()
    release = os.getenv("LAZYCLOUD_E2E_KUBERNETES_RELEASE", "").strip()
    if not all((context, namespace, release)):
        _blocked("explicit prepared-cluster context, namespace, and release are required")
        return None
    return context, namespace, release


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


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
