"""Prove two containers sharing one image each get their own writable root.

Image archives are global rather than workspace-scoped, so a writable root
shared by image id leaks one container's files to every later container on that
image. Both Functions here run on the same image, and therefore the same lower
layer.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack. Creates one uniquely named app and deletes it through the
public resource API.
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
        profile = require_live(argv, description=__doc__ or "container rootfs isolation")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_rootfs_isolation import (
        APP_NAME,
        ROOT_CANARY,
        TMP_CANARY,
        app,
        canary_reader,
        canary_writer,
    )

    workspace = profile.workspace
    secret = f"lazycloud-canary-{secrets.token_hex(16)}"
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        written = canary_writer.remote(
            root_path=ROOT_CANARY,
            tmp_path=TMP_CANARY,
            secret=secret,
        )
        read_back = canary_reader.remote(root_path=ROOT_CANARY, tmp_path=TMP_CANARY)
        _assert_distinct_containers(written, read_back)
        _assert_nothing_leaked(read_back, secret, paths=(ROOT_CANARY, TMP_CANARY))
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "worker.container_rootfs_isolation",
                    "writer_container": written["container"],
                    "reader_container": read_back["container"],
                },
                sort_keys=True,
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


def _assert_distinct_containers(written: dict[str, str], read_back: dict[str, str]) -> None:
    # A container's hostname is its id, so one name serving both calls means the
    # read never crossed a container boundary and proves nothing.
    if written["container"] == read_back["container"]:
        raise RuntimeError(
            f"both calls ran in container {written['container']}, "
            "so cross-container isolation was never exercised"
        )


def _assert_nothing_leaked(
    read_back: dict[str, str], secret: str, *, paths: tuple[str, str]
) -> None:
    root_path, tmp_path = paths
    leaked = [
        path
        for path, content in (
            (root_path, read_back["root_canary"]),
            (tmp_path, read_back["tmp_canary"]),
        )
        if content
    ]
    if leaked:
        # Name the paths, never the value that crossed the boundary.
        raise RuntimeError(
            "a later container on the same image read an earlier container's writes at "
            + ", ".join(leaked)
            + "; the writable root is shared"
        )
    if secret in read_back.values():
        raise RuntimeError("the reader container observed the writer's secret")


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique rootfs isolation scenario app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("rootfs isolation scenario app remained active after deletion")


if __name__ == "__main__":
    raise SystemExit(main())
