"""Round-trip Python objects and arrays through deployed Functions.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The scenario creates and publicly deletes one unique app.
Run with `uv run --group workspace --with numpy python -m
tests.e2e.local.function.scenario_serialization --live`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique serialization app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("serialization app remained active after deletion")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function serialization")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    import numpy as np

    from .workload_serialization import (
        APP_NAME,
        OpaqueNumber,
        app,
        double_matrix,
        matrix,
        opaque_square,
    )

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        result = opaque_square.remote(OpaqueNumber(9))
        if not isinstance(result, OpaqueNumber) or result.square().value != 6561:
            raise RuntimeError("custom Python value did not round-trip")
        if opaque_square.remote().value != 81:
            raise RuntimeError("Python default was not preserved")
        if asyncio.run(opaque_square.async_remote(OpaqueNumber(4))).value != 16:
            raise RuntimeError("async Python result did not round-trip")
        mapped = list(opaque_square.map([OpaqueNumber(2), OpaqueNumber(3)]))
        if [item.value if item is not None else None for item in mapped] != [4, 9]:
            raise RuntimeError("mapped Python results did not round-trip")
        downstream = opaque_square.spawn(opaque_square.spawn(OpaqueNumber(2)))
        if downstream.get(timeout_seconds=120).value != 16:
            raise RuntimeError("Python dependency did not round-trip")
        expected = np.arange(6, dtype=np.int64).reshape(2, 3)
        array = matrix.remote()
        if not isinstance(array, np.ndarray) or array.dtype != expected.dtype:
            raise RuntimeError("array result type or dtype changed")
        np.testing.assert_array_equal(array, expected)
        np.testing.assert_array_equal(double_matrix.remote(array), expected * 2)
        np.testing.assert_array_equal(
            double_matrix.spawn(matrix.spawn()).get(timeout_seconds=120), expected * 2
        )
        print(
            json.dumps(
                {"app": APP_NAME, "capability": "function.serialization", "result": result.value}
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
