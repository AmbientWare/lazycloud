from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from shared.http.task_payload import HttpTaskPayload, serialize_http_task_payload
from shared.tasks import (
    Task,
    TaskAttempt,
)


def test_task_json_fields_reject_unencoded_python_values() -> None:
    with pytest.raises(ValidationError):
        Task.model_validate(
            {
                "id": "task-invalid",
                "name": "invalid",
                "args": [datetime(2026, 7, 19, tzinfo=timezone.utc)],
            }
        )
    with pytest.raises(ValidationError):
        TaskAttempt.model_validate(
            {
                "id": "attempt-invalid",
                "task_id": "task-invalid",
                "attempt_number": 1,
                "result": datetime(2026, 7, 19, tzinfo=timezone.utc),
            }
        )


def test_http_task_payload_round_trips_nested_json_and_query_values() -> None:
    payload = serialize_http_task_payload(
        b'{"args":[1,"two",true,null,{"nested":[3.5]}],"kwargs":{"enabled":false}}',
        query_params={"count": ["4"], "labels": ["a", "b"]},
    )

    assert payload.args == [1, "two", True, None, {"nested": [3.5]}]
    assert payload.kwargs == {"enabled": False, "count": 4.0, "labels": ["a", "b"]}
    assert HttpTaskPayload.model_validate_json(payload.model_dump_json()) == payload
    assert serialize_http_task_payload().args is None
    assert serialize_http_task_payload().kwargs == {}


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null"])
def test_http_task_payload_rejects_invalid_or_non_object_json(body: bytes) -> None:
    with pytest.raises(ValueError):
        serialize_http_task_payload(body)
