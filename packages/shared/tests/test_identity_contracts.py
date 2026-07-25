from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from shared.identity import (
    ConcurrencyLimitRecord,
    WorkspaceRecord,
    WorkspaceStorageConfig,
)


def test_identity_records_round_trip_nested_json_without_default_loss() -> None:
    workspace = WorkspaceRecord(
        id="workspace-1",
        name="production",
        storage=WorkspaceStorageConfig(
            backend="s3",
            bucket="workspace-data",
            config={
                "endpoint_url": "https://storage.example.com",
                "force_path_style": False,
                "retry_delays": [0, 1.5],
                "headers": {"x-workspace": "production"},
            },
        ),
        metadata={"tier": 0, "features": ["private-compute"], "deleted": None},
    )

    payload = workspace.model_dump(mode="json")
    hydrated = WorkspaceRecord.model_validate_json(workspace.model_dump_json())

    assert hydrated == workspace
    assert payload["metadata"] == {
        "tier": 0,
        "features": ["private-compute"],
        "deleted": None,
    }
    assert payload["storage"]["config"]["force_path_style"] is False


def test_identity_json_boundaries_reject_non_json_metadata_and_storage_config() -> None:
    invalid_value = datetime(2026, 7, 19, tzinfo=timezone.utc)

    with pytest.raises(ValidationError):
        WorkspaceRecord.model_validate(
            {
                "id": "workspace-1",
                "name": "production",
                "metadata": {"seen": invalid_value},
            }
        )
    with pytest.raises(ValidationError):
        WorkspaceStorageConfig.model_validate({"config": {"seen": invalid_value}})


def test_concurrency_limits_require_positive_capacity_and_nonnegative_usage() -> None:
    limit = ConcurrencyLimitRecord(
        id="limit-1",
        workspace_id="workspace-1",
        name="workspace",
        limit=2,
        in_flight=2,
        metadata={"source": "workspace-default"},
    )

    assert limit.saturated
    assert limit.available == 0
    with pytest.raises(ValidationError, match="limit must be greater than zero"):
        ConcurrencyLimitRecord(
            id="limit-2",
            workspace_id="workspace-1",
            name="workspace",
            limit=0,
        )
    with pytest.raises(ValidationError, match="in_flight cannot be negative"):
        ConcurrencyLimitRecord(
            id="limit-3",
            workspace_id="workspace-1",
            name="workspace",
            limit=1,
            in_flight=-1,
        )
