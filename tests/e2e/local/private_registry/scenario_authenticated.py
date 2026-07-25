"""Build and execute one Function from a prepared authenticated registry.

Prerequisites: an authenticated public lazycloud profile plus
LAZYCLOUD_E2E_PRIVATE_IMAGE, LAZYCLOUD_E2E_REGISTRY_USERNAME, and
LAZYCLOUD_E2E_REGISTRY_PASSWORD. The prepared registry is environment ownership;
this scenario creates and publicly deletes only its uniquely named app.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(
            argv,
            description=__doc__ or "authenticated private registry",
            required_env=(
                "LAZYCLOUD_E2E_PRIVATE_IMAGE",
                "LAZYCLOUD_E2E_REGISTRY_USERNAME",
                "LAZYCLOUD_E2E_REGISTRY_PASSWORD",
            ),
        )
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_authenticated import APP_NAME, app, private_image

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        marker = f"private-registry-{secrets.token_hex(10)}"
        if private_image.remote(marker) != marker:
            raise RuntimeError("private-registry Function returned the wrong marker")
        print(json.dumps({"app": APP_NAME, "capability": "private-registry.authenticated"}))
    finally:
        client = resource_client(workspace=workspace, timeout_seconds=30)
        matches = [item for item in client.list_apps(active=True).data if item.name == APP_NAME]
        if len(matches) > 1:
            raise RuntimeError("unique private-registry app resolved more than once")
        if matches:
            client.delete_app(matches[0].id)
        if any(item.name == APP_NAME for item in client.list_apps(active=True).data):
            raise RuntimeError("private-registry app remained active after deletion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
