from __future__ import annotations

import pytest
from pydantic import JsonValue, ValidationError
from runner.function import decode_function_invocation
from runner.invocation import cloudpickle_bytes, invoke_handler
from shared.function_payloads import (
    FUNCTION_MARKER_MAX_DEPTH,
    FunctionCloudpickleInvocation,
    FunctionDependencyBinding,
    FunctionJsonInvocation,
    FunctionJsonResult,
    FunctionPayloadEncoding,
)
from shared.http.functions import FUNCTION_CALL_REF_MARKER, FunctionClaimedTask


class PythonValue:
    pass


def test_python_invocation_preserves_objects_while_json_coerces_arguments() -> None:
    def echo(value: PythonValue) -> PythonValue:
        return value

    def number(value: int) -> int:
        return value

    value = PythonValue()
    assert invoke_handler(echo, (value,), {}, encoding=FunctionPayloadEncoding.Cloudpickle) is value
    assert invoke_handler(number, ("7",), {}, encoding=FunctionPayloadEncoding.Json) == 7


def test_runner_substitutes_only_declared_exact_dependency_markers() -> None:
    marker: dict[str, JsonValue] = {FUNCTION_CALL_REF_MARKER: True, "task_id": "upstream"}
    response = _response(
        FunctionJsonInvocation(args=[marker], kwargs={"nested": [marker]}),
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
        FunctionJsonInvocation(args=[{FUNCTION_CALL_REF_MARKER: True, "task_id": "undeclared"}])
    )
    unused = _response(
        FunctionJsonInvocation(),
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


def test_runner_json_dependency_traversal_is_depth_bounded() -> None:
    nested: list[JsonValue] = []
    current = nested
    for _ in range(FUNCTION_MARKER_MAX_DEPTH + 2):
        child: list[JsonValue] = []
        current.append(child)
        current = child
    with pytest.raises(ValueError, match="marker depth exceeds"):
        decode_function_invocation(_response(FunctionJsonInvocation(args=[nested])))


def test_legacy_python_invocation_is_rejected_before_decoding() -> None:
    payload = FunctionCloudpickleInvocation.from_bytes(
        cloudpickle_bytes({"args": (), "kwargs": {}})
    )
    legacy = payload.model_dump(mode="json")
    legacy["version"] = 1
    with pytest.raises(ValidationError, match="version"):
        FunctionClaimedTask.model_validate({"task_id": "legacy", "invocation": legacy})


def _response(
    payload: FunctionJsonInvocation,
    *,
    bindings: list[FunctionDependencyBinding] | None = None,
) -> FunctionClaimedTask:
    return FunctionClaimedTask(
        task_id="task-1",
        invocation=payload,
        dependencies=bindings or [],
    )
