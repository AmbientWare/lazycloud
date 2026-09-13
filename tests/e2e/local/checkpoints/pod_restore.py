"""Prove one raw Pod resumes its in-memory process after a public checkpoint."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from lazycloud.clients.resource.control import ResourceControlClient
from pydantic import TypeAdapter
from shared.containers import ContainerStatus

BLOCKED = 77
SOURCE_ROOT = Path(__file__).resolve().parent
_STATE = TypeAdapter(dict[str, str | int])


def main() -> int:
    if "--live" not in sys.argv:
        return _blocked("checkpoint acceptance requires --live")
    endpoint, token, workspace = _prerequisites()
    app_name = f"e2e_checkpoint_pod_{uuid4().hex[:8]}"
    os.environ["LAZYCLOUD_E2E_CHECKPOINT_APP"] = app_name
    from .workloads import pod

    client = ResourceControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    primary_error: BaseException | None = None
    evidence: dict[str, object] = {}
    readiness_rejected = False
    try:
        deployed = pod.deploy(
            workspace=workspace,
            external_url=endpoint,
            source_root=SOURCE_ROOT,
        )
        client.scale_deployment(deployed.deployment_id, 1)
        source = _running_container(client, deployed.stub_id)
        first = _pod_state(deployed.invoke_url, token, "source")
        time.sleep(0.25)
        later = _pod_state(deployed.invoke_url, token, "checkpoint")
        _same_process(first, later)
        control_url = deployed.invoke_url.rstrip("/") + "/readiness/"
        headers = {"Authorization": f"Bearer {token}"}
        httpx.post(control_url + "disable", headers=headers, timeout=5).raise_for_status()
        response = httpx.get(deployed.invoke_url.rstrip("/") + "/ready", headers=headers, timeout=5)
        assert response.status_code == 503
        rejected_id = str(uuid4())
        rejected = _checkpoint(source, workspace, checkpoint_id=rejected_id)
        readiness_rejected = rejected.returncode != 0
        if readiness_rejected:
            assert "readiness" in rejected.stderr.lower() and "503" in rejected.stderr
            _same_process(later, _pod_state(deployed.invoke_url, token, "after-rejection"))
        print(
            json.dumps(
                {
                    "case": "checkpoint-unready",
                    "container": source,
                    "checkpoint": rejected_id,
                    "cli_exit": rejected.returncode,
                    "rejected": readiness_rejected,
                }
            ),
            flush=True,
        )
        httpx.post(control_url + "enable", headers=headers, timeout=5).raise_for_status()
        selected_id = str(uuid4())
        created = _checkpoint(source, workspace, checkpoint_id=selected_id)
        if created.returncode != 0:
            raise RuntimeError(created.stderr.strip() or "public checkpoint command failed")
        assert json.loads(created.stdout)["checkpoint_id"] == selected_id
        client.stop_container(source)
        replacement = _running_container(client, deployed.stub_id, exclude={source})
        restored = _pod_state(deployed.invoke_url, token, "restored")
        _same_process(later, restored)
        if int(restored["counter"]) < int(later["counter"]):
            raise RuntimeError("restored Pod counter regressed")
        evidence = {
            "source_container_id": source,
            "restored_container_id": replacement,
            "boot_id": restored["boot_id"],
            "counter_before": later["counter"],
            "counter_after": restored["counter"],
        }
    except BaseException as exc:
        primary_error = exc
    cleanup_error = _delete_app(client, app_name)
    if primary_error is not None:
        if cleanup_error:
            raise RuntimeError(
                f"Pod restore failed and cleanup failed: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error:
        raise RuntimeError(f"Pod restore cleanup failed: {cleanup_error}")
    assert readiness_rejected, "manual checkpoint ignored the workload's failed readiness probe"
    print(json.dumps({"accepted": True, "evidence": evidence}, sort_keys=True))
    return 0


def _prerequisites() -> tuple[str, str, str]:
    endpoint = os.getenv("LAZYCLOUD_ENDPOINT", "").rstrip("/")
    token = os.getenv("LAZYCLOUD_TOKEN", "")
    workspace = os.getenv("LAZYCLOUD_WORKSPACE", "default")
    if not endpoint or not token:
        raise SystemExit(_blocked("LAZYCLOUD_ENDPOINT and LAZYCLOUD_TOKEN are required"))
    try:
        httpx.get(f"{endpoint}/health", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        raise SystemExit(_blocked(f"prepared control plane is unavailable: {exc}")) from exc
    return endpoint, token, workspace


def _pod_state(url: str, token: str, label: str) -> dict[str, str | int]:
    response = httpx.get(
        f"{url.rstrip('/')}/state",
        params={label: ""},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    try:
        state = _STATE.validate_json(response.content)
    except ValueError as exc:
        raise RuntimeError("checkpoint Pod returned an invalid object") from exc
    required = {"boot_id", "born_at_ns", "counter", "pid"}
    if not required.issubset(state):
        raise RuntimeError("checkpoint Pod omitted continuity state")
    return state


def _same_process(before: dict[str, str | int], after: dict[str, str | int]) -> None:
    for field in ("boot_id", "born_at_ns", "pid"):
        if before[field] != after[field]:
            raise RuntimeError(f"checkpoint restoration changed process field {field}")


def _running_container(
    client: ResourceControlClient,
    stub_id: str,
    *,
    exclude: set[str] | None = None,
) -> str:
    deadline = time.monotonic() + 120
    excluded = exclude or set()
    while time.monotonic() < deadline:
        containers = client.list_containers(stub_ids=(stub_id,)).data
        print(
            json.dumps(
                {
                    "case": "checkpoint-containers",
                    "containers": [
                        (item.container.id, item.container.status.value) for item in containers
                    ],
                }
            ),
            flush=True,
        )
        running = [
            item.container.id
            for item in containers
            if item.container.status is ContainerStatus.Running
            and item.container.id not in excluded
        ]
        if len(running) == 1:
            return running[0]
        time.sleep(0.5)
    raise RuntimeError("checkpoint Pod did not expose one running container")


def _checkpoint(
    container_id: str, workspace: str, *, checkpoint_id: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "uv",
            "run",
            "lazycloud",
            "--json",
            "container",
            "checkpoint",
            container_id,
            "--checkpoint-id",
            checkpoint_id,
            "--workspace",
            workspace,
        ],
        text=True,
        capture_output=True,
        check=False,
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
        return str(exc)
    return ""


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
