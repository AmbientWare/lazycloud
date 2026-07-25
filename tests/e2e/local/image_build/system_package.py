"""Opt-in proof that one system-package image builds and executes."""

from __future__ import annotations

import json
import os
import sys
from uuid import uuid4

import httpx
from lazycloud.clients.resource.control import ResourceControlClient

from lazycloud import App, Image

BLOCKED = 77
APP_NAME = f"e2e_image_system_{uuid4().hex[:10]}"
app = App(APP_NAME)


@app.function(
    name="system-packages",
    image=Image(python_version="python3.12").add_commands(
        ["apt-get update && apt-get install -y ffmpeg git"]
    ),
)
def system_packages() -> list[str]:
    import shutil

    return [name for name in ("ffmpeg", "git") if shutil.which(name)]


def main() -> int:
    if "--live" not in sys.argv and os.getenv("LAZYCLOUD_E2E_IMAGE_BUILD_LIVE") != "1":
        return _blocked("live image-build acceptance requires --live")
    endpoint, token, workspace = _prerequisites()
    result: list[str] | None = None
    primary_error: BaseException | None = None
    try:
        result = system_packages.remote()
        if result != ["ffmpeg", "git"]:
            raise RuntimeError("built image did not contain both requested system packages")
    except BaseException as exc:
        primary_error = exc
    cleanup_error = _delete_owned_app(endpoint, token, workspace)
    if primary_error is not None:
        if cleanup_error:
            raise RuntimeError(
                f"system-package execution failed and cleanup failed: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error:
        raise RuntimeError(f"system-package app cleanup failed: {cleanup_error}")
    print(
        json.dumps(
            {
                "accepted": True,
                "app": APP_NAME,
                "evidence": result,
                "cleanup": "owned app deleted",
            },
            sort_keys=True,
        )
    )
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


def _delete_owned_app(endpoint: str, token: str, workspace: str) -> str:
    try:
        client = ResourceControlClient.from_endpoint(
            endpoint,
            token=token,
            workspace=workspace,
        )
        matches = [item for item in client.list_apps().data if item.name == APP_NAME]
        if len(matches) > 1:
            return f"multiple apps unexpectedly matched unique name {APP_NAME}"
        if matches:
            client.delete_app(matches[0].id)
        if any(item.name == APP_NAME for item in client.list_apps().data):
            return f"app {APP_NAME} remains after public deletion"
    except Exception as exc:
        return str(exc)
    return ""


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
