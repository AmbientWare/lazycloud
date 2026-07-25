"""Execute a deployed private image after its disposable registry is stopped.

Prerequisites: an authenticated public lazycloud profile, the registry
credentials, and an exact LAZYCLOUD_E2E_PRIVATE_REGISTRY_CONTAINER created with the
`com.lazycloud.e2e=private-registry` label. The registry is always restarted;
the uniquely named app is publicly deleted.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from tests.e2e._support.process import (
    LivePrerequisiteError,
    blocked,
    require_live,
    run_text_process,
)

ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = Path(__file__).resolve().parent
CONTAINER_NAME = re.compile(r"lazycloud-e2e-registry-[a-z0-9][a-z0-9_.-]*")


def _guard_registry(container: str) -> None:
    if CONTAINER_NAME.fullmatch(container) is None:
        raise RuntimeError("refusing to stop an unowned private-registry container")
    result = run_text_process(
        (
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "com.lazycloud.e2e"}}',
            container,
        ),
        cwd=ROOT,
        environment=os.environ,
        timeout=30,
    )
    if result.stdout.strip() != "private-registry":
        raise RuntimeError("private-registry container is missing its ownership label")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(
            argv,
            description=__doc__ or "private registry archive after loss",
            required_env=(
                "LAZYCLOUD_E2E_PRIVATE_IMAGE",
                "LAZYCLOUD_E2E_PRIVATE_REGISTRY_CONTAINER",
                "LAZYCLOUD_E2E_REGISTRY_USERNAME",
                "LAZYCLOUD_E2E_REGISTRY_PASSWORD",
            ),
        )
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_archive import APP_NAME, app, archived_private_image

    workspace = profile.workspace
    registry = os.environ["LAZYCLOUD_E2E_PRIVATE_REGISTRY_CONTAINER"]
    stopped = False
    try:
        _guard_registry(registry)
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        marker = f"private-archive-{secrets.token_hex(10)}"
        if archived_private_image.remote(marker) != marker:
            raise RuntimeError("private image failed before registry loss")
        run_text_process(
            ("docker", "stop", registry),
            cwd=ROOT,
            environment=os.environ,
            timeout=30,
        )
        stopped = True
        if archived_private_image.remote(marker) != marker:
            raise RuntimeError("durable private image failed after registry loss")
        print(json.dumps({"app": APP_NAME, "capability": "private-registry.archive-after-loss"}))
    finally:
        if stopped:
            run_text_process(
                ("docker", "start", registry),
                cwd=ROOT,
                environment=os.environ,
                timeout=30,
            )
        client = resource_client(workspace=workspace, timeout_seconds=30)
        matches = [item for item in client.list_apps(active=True).data if item.name == APP_NAME]
        if len(matches) > 1:
            raise RuntimeError("unique private archive app resolved more than once")
        if matches:
            client.delete_app(matches[0].id)
        if any(item.name == APP_NAME for item in client.list_apps(active=True).data):
            raise RuntimeError("private archive app remained active after deletion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
