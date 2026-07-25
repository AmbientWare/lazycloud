"""Save, stat, and publicly read one Output owned by a real Function task.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario creates and publicly deletes one uniquely named app.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from pathlib import Path

import httpx
from lazycloud.cli.control import resource_client
from lazycloud.clients.output.control import OutputControlClient
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent


def _delete_app(workspace: str, app_name: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == app_name]
    if len(matches) > 1:
        raise RuntimeError("unique Output app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == app_name for item in client.list_apps(active=True).data):
        raise RuntimeError("Output app remained active after deletion")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Output")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_output import APP_NAME, app, output_owner

    endpoint = profile.resolved_endpoint().rstrip("/")
    workspace = profile.workspace
    marker = f"output-{secrets.token_hex(12)}"
    try:
        app.deploy(
            workspace=workspace,
            source_root=SOURCE_ROOT,
            env={"LAZYCLOUD_E2E_APP": APP_NAME},
        )
        call = output_owner.spawn(marker)
        if call.get(timeout_seconds=120, poll_interval_seconds=0.5) != marker:
            raise RuntimeError("Output owner Function returned the wrong marker")
        outputs = OutputControlClient.from_endpoint(
            endpoint,
            token=profile.token,
            timeout_seconds=30,
            workspace=workspace,
        )
        content = marker.encode()
        saved = outputs.save(
            call.task_id,
            "accepted.txt",
            content,
            content_type="text/plain",
        )
        stat = outputs.stat(saved.id, call.task_id, "accepted.txt")
        if stat.stat is None or stat.stat.size != len(content):
            raise RuntimeError("Output stat returned the wrong size")
        public = outputs.public_url(
            saved.id,
            call.task_id,
            "accepted.txt",
            gateway_external_url=endpoint,
        )
        response = httpx.get(public.public_url, timeout=30)
        response.raise_for_status()
        if response.content != content:
            raise RuntimeError("Output public URL returned the wrong bytes")
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "storage.output",
                    "output_id": saved.id,
                    "task_id": call.task_id,
                }
            )
        )
    finally:
        _delete_app(workspace, APP_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
