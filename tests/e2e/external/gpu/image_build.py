"""Opt-in proof that one GPU image builds and executes on prepared GPU capacity."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable

from lazycloud.abstractions.function import Function
from lazycloud.clients.resource.control import ResourceControlClient
from tests.e2e.external import _support
from tests.e2e.external.gpu.workloads import (
    APP_NAME,
    CASE_TIMEOUT_SECONDS,
    cuda_runtime,
    cuda_torch,
    pytorch_cuda,
)

CASES: dict[str, tuple[Function[[], str], Callable[[str], bool]]] = {
    "cuda-runtime": (cuda_runtime, bool),
    "cuda-torch": (cuda_torch, lambda value: value == "cuda:True"),
    "pytorch-cuda": (
        pytorch_cuda,
        lambda value: value == "cuda:True:ffmpeg:True",
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=sorted(CASES), required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--timeout", type=float, default=CASE_TIMEOUT_SECONDS)
    args = parser.parse_args()
    if not args.live or os.getenv("LAZYCLOUD_E2E_GPU_ACKNOWLEDGED") != "1":
        return _support.skip(
            "GPU image build", "--live and LAZYCLOUD_E2E_GPU_ACKNOWLEDGED=1 are required"
        )
    try:
        endpoint, token, workspace = _support.prepared_gateway()
    except _support.MissingPrerequisite as exc:
        return _support.skip("GPU image build", exc)

    runner, accepted = CASES[args.case]
    result: str | None = None
    task_id = ""
    primary_error: BaseException | None = None
    try:
        call = runner.spawn()
        task_id = call.task_id
        result = call.get(timeout_seconds=args.timeout)
        if not accepted(result):
            raise RuntimeError(f"{args.case} returned unexpected GPU evidence")
    except BaseException as exc:
        primary_error = exc
        print(f"recovery: GPU app {APP_NAME}, task {task_id or 'not submitted'}", file=sys.stderr)
    cleanup_error = _delete_owned_app(endpoint, token, workspace)
    if primary_error is not None:
        if cleanup_error:
            raise RuntimeError(
                f"{args.case} failed and app cleanup also failed: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error:
        raise RuntimeError(f"{args.case} app cleanup failed: {cleanup_error}")
    _support.emit_evidence(
        {
            "accepted": True,
            "app": APP_NAME,
            "case": args.case,
            "cleanup": "owned app deleted",
            "evidence": result,
            "task_id": task_id,
        }
    )
    return 0


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
        return f"app {APP_NAME}: {exc}"
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
