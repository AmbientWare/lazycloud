"""Opt-in proof that one pinned Python package builds and executes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from uuid import uuid4

import httpx
from lazycloud.abstractions.function import Function
from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.control import control_workspace_scope, resolve_control_client_config
from tests.e2e.local.image_build.python_functions import python_image_functions

BLOCKED = 77
APP_NAME = f"e2e_image_python_{uuid4().hex[:10]}"
FUNCTIONS = python_image_functions(APP_NAME)


CASES: dict[str, tuple[Function[[], str], Callable[[str], bool]]] = {
    "python312-base": (FUNCTIONS["python312-base"], lambda value: value == "python:3.12"),
    "python310-numpy": (FUNCTIONS["python310-numpy"], lambda value: value == "numpy:7"),
    "python311-httpx": (FUNCTIONS["python311-httpx"], lambda value: value == "httpx:0.28.1"),
    "python312-packaging": (
        FUNCTIONS["python312-packaging"],
        lambda value: value == "packaging:24.2",
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=sorted(CASES), required=True)
    parser.add_argument(
        "--live",
        action="store_true",
        default=os.getenv("LAZYCLOUD_E2E_IMAGE_BUILD_LIVE") == "1",
    )
    args = parser.parse_args()
    if not args.live:
        return _blocked("live image-build acceptance requires --live")
    endpoint, token, workspace = _prerequisites()
    runner, accepted = CASES[args.case]
    result: str | None = None
    primary_error: BaseException | None = None
    try:
        with control_workspace_scope(workspace):
            result = runner.remote()
        if not accepted(result):
            raise RuntimeError(f"{args.case} returned an unexpected result")
    except BaseException as exc:
        primary_error = exc

    cleanup_error = _delete_owned_app(endpoint, token, workspace)
    if primary_error is not None:
        if cleanup_error:
            raise RuntimeError(
                f"{args.case} failed and app cleanup also failed: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error:
        raise RuntimeError(f"{args.case} app cleanup failed: {cleanup_error}")
    print(
        json.dumps(
            {
                "accepted": True,
                "app": APP_NAME,
                "case": args.case,
                "evidence": result,
                "cleanup": "owned app deleted",
            },
            sort_keys=True,
        )
    )
    return 0


def _prerequisites() -> tuple[str, str, str]:
    config = resolve_control_client_config()
    endpoint = config.endpoint.rstrip("/")
    token = config.token or ""
    workspace = config.workspace
    expected_workspace = os.getenv("LAZYCLOUD_E2E_WORKSPACE", "").strip()
    if expected_workspace and workspace != expected_workspace:
        raise SystemExit(_blocked("active SDK profile does not match LAZYCLOUD_E2E_WORKSPACE"))
    if not endpoint or not token:
        raise SystemExit(_blocked("an authenticated LazyCloud profile is required"))
    try:
        response = httpx.get(f"{endpoint}/health", timeout=5)
        response.raise_for_status()
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
