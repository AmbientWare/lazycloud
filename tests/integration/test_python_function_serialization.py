from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, NoReturn

import pytest
from lazycloud.abstractions.function import FunctionOperationError, _serialize_invocation
from lazycloud.session.task import FunctionCall, Task
from runner.function import decode_function_invocation
from runner.invocation import cloudpickle_bytes, invoke_handler
from shared.function_payloads import (
    FunctionCloudpickleInvocation,
    FunctionCloudpickleResult,
    FunctionDependencyBinding,
)
from shared.http.functions import FunctionClaimedTask


class Point(NamedTuple):
    x: int
    y: int


class Nested(NamedTuple):
    point: Point
    dependency: object


class ArgumentList(list[object]):
    dependency: object


@dataclass
class Box:
    dependency: object
    cycle: Box | None = None


class UnserializableClient:
    def handle(self, task_id: str) -> Task:
        raise AssertionError("dependency client must not be called")

    def rerun(self, task_id: str) -> Task:
        raise AssertionError("dependency client must not be called")

    def __reduce__(self) -> NoReturn:
        raise AssertionError("dependency client state must never be serialized")


def test_python_invocation_preserves_object_graph_and_resolves_nested_calls() -> None:
    call = FunctionCall[Point](
        task_id="upstream", workspace_id="workspace", client=UnserializableClient()
    )
    values = ArgumentList([call])
    values.dependency = call
    values.append(values)
    box = Box(dependency=call)
    box.cycle = box
    serialized = _serialize_invocation(
        (Nested(Point(2, 3), call), values, values, box), {"alias": values}
    )
    assert [dependency.task_id for dependency in serialized.dependencies] == ["upstream"]
    assert serialized.dependencies[0].workspace_id == "workspace"
    response = FunctionClaimedTask(
        task_id="downstream",
        invocation=serialized.payload,
        dependencies=[
            FunctionDependencyBinding(
                task_id="upstream",
                result=FunctionCloudpickleResult.from_bytes(cloudpickle_bytes(Point(4, 5))),
            )
        ],
    )

    def inspect_graph(
        nested: Nested,
        first: ArgumentList,
        second: ArgumentList,
        box: Box,
        *,
        alias: ArgumentList,
    ) -> int:
        assert first is second is alias
        assert first[1] is first
        assert box.cycle is box
        assert first.dependency is first[0] is box.dependency is nested.dependency
        assert isinstance(nested.dependency, Point)
        return nested.point.x + nested.dependency.y

    invocation = decode_function_invocation(response)
    assert (
        invoke_handler(
            inspect_graph,
            invocation.args,
            invocation.kwargs,
            encoding=invocation.argument_encoding,
        )
        == 7
    )


def test_python_dependencies_reject_missing_unused_and_malformed_references() -> None:
    call = FunctionCall[Point](task_id="upstream", client=UnserializableClient())
    serialized = _serialize_invocation((call,), {})
    with pytest.raises(ValueError, match="undeclared function dependency"):
        decode_function_invocation(
            FunctionClaimedTask(task_id="missing", invocation=serialized.payload)
        )
    with pytest.raises(ValueError, match="unused function dependency"):
        decode_function_invocation(
            FunctionClaimedTask(
                task_id="unused",
                invocation=_serialize_invocation((), {}).payload,
                dependencies=[
                    FunctionDependencyBinding(
                        task_id="upstream",
                        result=FunctionCloudpickleResult.from_bytes(cloudpickle_bytes(1)),
                    )
                ],
            )
        )
    with pytest.raises(ValueError, match="invalid function dependency reference"):
        decode_function_invocation(
            FunctionClaimedTask(
                task_id="malformed",
                invocation=FunctionCloudpickleInvocation.from_bytes(b"Puntagged-reference\n."),
            )
        )


def test_python_dependencies_reject_conflicting_workspace_claims() -> None:
    with pytest.raises(FunctionOperationError, match="conflicting workspaces"):
        _serialize_invocation(
            (
                FunctionCall[Point](
                    task_id="upstream", workspace_id="first", client=UnserializableClient()
                ),
                FunctionCall[Point](
                    task_id="upstream", workspace_id="second", client=UnserializableClient()
                ),
            ),
            {},
        )
