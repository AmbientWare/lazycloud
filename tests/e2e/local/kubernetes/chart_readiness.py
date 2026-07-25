"""Verify an explicitly selected prepared Helm release is publicly ready."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import httpx
from pydantic import BaseModel, ConfigDict, Field

BLOCKED = 77


class HelmInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str


class HelmStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    info: HelmInfo


class Metadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class DeploymentSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    replicas: int = 0


class DeploymentStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    available_replicas: int = Field(default=0, alias="availableReplicas")


class Deployment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    metadata: Metadata
    spec: DeploymentSpec
    status: DeploymentStatus = Field(default_factory=DeploymentStatus)


class DeploymentList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[Deployment] = Field(default_factory=list)


def main() -> int:
    if "--live" not in sys.argv:
        return _blocked("Kubernetes readiness acceptance requires --live")
    target = _target()
    if target is None:
        return BLOCKED
    context, namespace, release, endpoint = target
    status = HelmStatus.model_validate_json(
        _output(
            [
                "helm",
                "status",
                release,
                "--namespace",
                namespace,
                "--kube-context",
                context,
                "--output",
                "json",
            ]
        )
    )
    if status.info.status != "deployed":
        raise RuntimeError(f"Helm release {release} is not deployed")
    deployments = DeploymentList.model_validate_json(
        _output(
            [
                "kubectl",
                "--context",
                context,
                "--namespace",
                namespace,
                "get",
                "deployments",
                "-l",
                f"app.kubernetes.io/instance={release}",
                "-o",
                "json",
            ]
        )
    ).items
    if not deployments:
        raise RuntimeError("selected Helm release has no deployments")
    unavailable = [
        item.metadata.name
        for item in deployments
        if item.status.available_replicas != item.spec.replicas
    ]
    if unavailable:
        raise RuntimeError("unready release deployments: " + ", ".join(unavailable))
    try:
        health = httpx.get(f"{endpoint}/health", timeout=5)
        health.raise_for_status()
    except httpx.HTTPError as exc:
        return _blocked(f"prepared public endpoint is unavailable: {exc}")
    print(
        json.dumps(
            {
                "accepted": True,
                "context": context,
                "namespace": namespace,
                "release": release,
                "ready_deployments": len(deployments),
                "public_health": health.status_code,
                "cleanup": "read-only; no resources created",
            },
            sort_keys=True,
        )
    )
    return 0


def _target() -> tuple[str, str, str, str] | None:
    for command in ("helm", "kubectl"):
        if shutil.which(command) is None:
            _blocked(f"{command} is required")
            return None
    context = os.getenv("LAZYCLOUD_E2E_KUBERNETES_CONTEXT", "").strip()
    namespace = os.getenv("LAZYCLOUD_E2E_KUBERNETES_NAMESPACE", "").strip()
    release = os.getenv("LAZYCLOUD_E2E_KUBERNETES_RELEASE", "").strip()
    endpoint = os.getenv("LAZYCLOUD_ENDPOINT", "").strip()
    if not all((context, namespace, release, endpoint)):
        _blocked(
            "explicit Kubernetes context, namespace, release, and LAZYCLOUD_ENDPOINT are required"
        )
        return None
    return context, namespace, release, endpoint


def _output(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"command failed: {command[0]}")
    return completed.stdout


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
