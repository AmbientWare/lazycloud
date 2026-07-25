"""Verify a separately restarted scheduler retained durable worker sizing."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field

BLOCKED = 77


class Metadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class PodCondition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    condition_type: str = Field(alias="type")
    status: str


class PodStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start_time: datetime = Field(alias="startTime")
    conditions: list[PodCondition] = Field(default_factory=list)


class Pod(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: Metadata
    status: PodStatus


class PodList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[Pod] = Field(default_factory=list)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restarted-after", type=datetime.fromisoformat, required=True)
    parser.add_argument("--expected-worker-replicas", type=int, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _blocked("scheduler-restart acceptance requires --live")
    target = _target()
    if target is None:
        return BLOCKED
    context, namespace, release, endpoint = target
    if args.restarted_after.tzinfo is None:
        raise RuntimeError("--restarted-after must include a timezone")
    pods = PodList.model_validate_json(
        _output(
            [
                "kubectl",
                "--context",
                context,
                "--namespace",
                namespace,
                "get",
                "pods",
                "-l",
                (f"app.kubernetes.io/instance={release},app.kubernetes.io/component=scheduler"),
                "-o",
                "json",
            ]
        )
    ).items
    if len(pods) != 1:
        raise RuntimeError("expected exactly one scheduler Pod after operator restart")
    pod = pods[0]
    started = pod.status.start_time
    ready = any(
        condition.condition_type == "Ready" and condition.status == "True"
        for condition in pod.status.conditions
    )
    if started <= args.restarted_after or not ready:
        raise RuntimeError("scheduler Pod does not prove a completed restart")
    desired = _worker_replicas(context, namespace, release)
    if desired != args.expected_worker_replicas:
        raise RuntimeError(
            "scheduler restart changed worker replicas: "
            f"{args.expected_worker_replicas} -> {desired}"
        )
    try:
        httpx.get(f"{endpoint}/health", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError("public endpoint failed after scheduler restart") from exc
    print(
        json.dumps(
            {
                "accepted": True,
                "scheduler_pod": pod.metadata.name,
                "scheduler_started_at": started.isoformat(),
                "worker_replicas": desired,
                "cleanup": "read-only; operator-owned release retained",
            },
            sort_keys=True,
        )
    )
    return 0


def _target() -> tuple[str, str, str, str] | None:
    context = os.getenv("LAZYCLOUD_E2E_KUBERNETES_CONTEXT", "").strip()
    namespace = os.getenv("LAZYCLOUD_E2E_KUBERNETES_NAMESPACE", "").strip()
    release = os.getenv("LAZYCLOUD_E2E_KUBERNETES_RELEASE", "").strip()
    endpoint = os.getenv("LAZYCLOUD_ENDPOINT", "").strip()
    if not all((context, namespace, release, endpoint)):
        _blocked("explicit prepared-cluster context, namespace, release, and endpoint are required")
        return None
    return context, namespace, release, endpoint


def _worker_replicas(context: str, namespace: str, release: str) -> int:
    fullname = release if "lazycloud" in release else f"{release}-lazycloud"
    deployment = f"{fullname[:63].rstrip('-')}-container-worker-default"
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


def _output(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "kubectl command failed")
    return completed.stdout


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
