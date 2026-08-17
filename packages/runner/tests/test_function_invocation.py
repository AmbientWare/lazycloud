from __future__ import annotations

from typing import Any

import pytest
from runner.function import decode_function_invocation
from runner.invocation import cloudpickle_bytes
from shared.function_payloads import (
    FUNCTION_MARKER_MAX_DEPTH,
    FunctionCloudpickleInvocation,
    FunctionDependencyBinding,
    FunctionJsonResult,
)
from shared.http.functions import FUNCTION_CALL_REF_MARKER, FunctionClaimedTask


def test_runner_substitutes_only_declared_exact_dependency_markers() -> None:
    marker: dict[str, Any] = {FUNCTION_CALL_REF_MARKER: True, "task_id": "upstream"}
    response = _response(
        {"args": (marker,), "kwargs": {"nested": [marker]}},
        bindings=[
            FunctionDependencyBinding(
                task_id="upstream",
                result=FunctionJsonResult(value={"answer": 42}),
            )
        ],
    )

    invocation = decode_function_invocation(response)

    assert invocation.args == ({"answer": 42},)
    assert invocation.kwargs == {"nested": [{"answer": 42}]}


def test_runner_rejects_undeclared_and_unused_dependency_bindings() -> None:
    undeclared = _response(
        {
            "args": ({FUNCTION_CALL_REF_MARKER: True, "task_id": "undeclared"},),
            "kwargs": {},
        }
    )
    unused = _response(
        {"args": (), "kwargs": {}},
        bindings=[
            FunctionDependencyBinding(
                task_id="upstream",
                result=FunctionJsonResult(value=1),
            )
        ],
    )

    with pytest.raises(ValueError, match="undeclared function dependency"):
        decode_function_invocation(undeclared)
    with pytest.raises(ValueError, match="unused function dependency"):
        decode_function_invocation(unused)


def test_runner_dependency_traversal_is_cycle_safe_and_depth_bounded() -> None:
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    cycle_response = _response({"args": (cyclic,), "kwargs": {}})

    cycle_invocation = decode_function_invocation(cycle_response)

    replaced_cycle = cycle_invocation.args[0]
    assert isinstance(replaced_cycle, list)
    assert replaced_cycle[0] is replaced_cycle

    nested: list[Any] = []
    current = nested
    for _ in range(FUNCTION_MARKER_MAX_DEPTH + 2):
        child: list[Any] = []
        current.append(child)
        current = child
    with pytest.raises(ValueError, match="marker depth exceeds"):
        decode_function_invocation(_response({"args": (nested,), "kwargs": {}}))


def _response(
    payload: dict[str, Any],
    *,
    bindings: list[FunctionDependencyBinding] | None = None,
) -> FunctionClaimedTask:
    return FunctionClaimedTask(
        task_id="task-1",
        invocation=FunctionCloudpickleInvocation.from_bytes(cloudpickle_bytes(payload)),
        dependencies=bindings or [],
    )
