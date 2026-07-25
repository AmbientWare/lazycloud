from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import JsonValue, TypeAdapter
from runner.taskqueue import serialize_task_queue_result

_JSON_VALUE = TypeAdapter[JsonValue](JsonValue)


@dataclass(frozen=True)
class _TaskResult:
    count: int
    labels: tuple[str, ...]


class _UnsupportedTaskResult:
    pass


def test_task_queue_result_does_not_fabricate_empty_success_for_unsupported_value() -> None:
    with pytest.raises(TypeError, match="unsupported JSON value type"):
        serialize_task_queue_result(_UnsupportedTaskResult())
