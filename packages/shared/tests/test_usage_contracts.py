from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue, ValidationError
from shared.http.usage import UsageRecordResponse
from shared.usage import (
    UsageMetric,
    UsageRecord,
    UsageUnit,
    usage_record_id,
    usage_to_openmeter_events,
)


def test_usage_contracts_preserve_recursive_json_metadata() -> None:
    created_at = datetime(2026, 7, 19, tzinfo=UTC)
    metadata: dict[str, JsonValue] = {
        "window": {
            "start": "2026-07-19T00:00:00Z",
            "samples": [1, 2.5, True, None],
        }
    }
    record = UsageRecord(
        id="usage-1",
        workspace_id="workspace-1",
        resource_type="container",
        resource_id="container-1",
        metric=UsageMetric.CpuSeconds,
        quantity=2.5,
        unit=UsageUnit.Seconds,
        metadata=metadata,
        created_at=created_at,
    )

    response = UsageRecordResponse.model_validate(record.model_dump(mode="json"))
    event = usage_to_openmeter_events([record])[0]

    assert response.metadata == metadata
    assert event.data["metadata"] == metadata
    assert event.model_dump(mode="json")["data"]["metadata"] == metadata


def test_usage_contracts_reject_non_json_metadata() -> None:
    with pytest.raises(ValidationError):
        UsageRecord.model_validate(
            {
                "id": "usage-1",
                "workspace_id": "workspace-1",
                "resource_type": "container",
                "resource_id": "container-1",
                "metric": UsageMetric.TaskCount,
                "quantity": 1,
                "unit": UsageUnit.Count,
                "metadata": {"invalid": {"not", "json"}},
            }
        )


def test_usage_identity_is_deterministic() -> None:
    assert usage_record_id("task_count", "workspace-1", 123) == usage_record_id(
        "task_count",
        "workspace-1",
        123,
    )
