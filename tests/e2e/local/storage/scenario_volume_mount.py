"""Read and write one Volume from a deployed Function mount.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario creates one unique app and Volume, then publicly deletes
both.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client, volume_client
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

from lazycloud import Volume

SOURCE_ROOT = Path(__file__).resolve().parent


def _cleanup(workspace: str, app_name: str, volume_name: str) -> None:
    resources = resource_client(workspace=workspace, timeout_seconds=30)
    apps = [item for item in resources.list_apps(active=True).data if item.name == app_name]
    if len(apps) > 1:
        raise RuntimeError("unique Volume mount app resolved more than once")
    if apps:
        resources.delete_app(apps[0].id)
    volumes = volume_client(workspace=workspace, timeout_seconds=30)
    if any(item.name == volume_name for item in volumes.list_volumes().volumes):
        volumes.delete(volume_name)
    if any(item.name == app_name for item in resources.list_apps(active=True).data):
        raise RuntimeError("Volume mount app remained active after deletion")
    if any(item.name == volume_name for item in volumes.list_volumes().volumes):
        raise RuntimeError("Volume mount resource remained after deletion")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Volume mount")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_volume_mount import APP_NAME, VOLUME_NAME, app, volume_probe

    workspace = profile.workspace
    marker = f"volume-mount-{secrets.token_hex(12)}"
    volume = Volume(VOLUME_NAME, workspace=workspace)
    try:
        created = volume_client(workspace=workspace, timeout_seconds=30).create(VOLUME_NAME)
        if created.volume is None:
            raise RuntimeError("Volume mount scenario did not create its Volume")
        app.deploy(
            workspace=workspace,
            source_root=SOURCE_ROOT,
            env={
                "LAZYCLOUD_E2E_APP": APP_NAME,
                "LAZYCLOUD_E2E_VOLUME": VOLUME_NAME,
            },
        )
        written = volume_probe.remote("write", "worker/value.txt", marker)
        if written != marker or volume.read_text("worker/value.txt") != marker:
            raise RuntimeError("Function Volume mount did not persist its write")
        volume.write_text("client/value.txt", marker)
        if volume_probe.remote("read", "client/value.txt") != marker:
            raise RuntimeError("Function Volume mount did not observe the client write")
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "storage.volume-mount",
                    "volume": VOLUME_NAME,
                }
            )
        )
    finally:
        _cleanup(workspace, APP_NAME, VOLUME_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
