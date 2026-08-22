from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from worker.repository_payloads import (
    AppendContainerLogsRequest,
    ContainerLogBatchEntry,
    ContainerLogEntryKind,
    ContainerLogStream,
)


def test_container_log_batch_rejects_sequence_gaps_and_invalid_kind_fields() -> None:
    with pytest.raises(ValidationError, match="contiguous and ordered"):
        AppendContainerLogsRequest(
            container_id="container-1",
            capture_id="capture-1",
            entries=[_entry(0, message="first"), _entry(2, message="gap")],
        )

    with pytest.raises(ValidationError, match="include dropped_count"):
        _entry(0, kind=ContainerLogEntryKind.Dropped, message="dropped")

    with pytest.raises(ValidationError, match="must not be empty"):
        _entry(0)


def _entry(
    sequence: int,
    *,
    stream: ContainerLogStream = ContainerLogStream.Stdout,
    message: str = "",
    kind: ContainerLogEntryKind = ContainerLogEntryKind.Output,
    dropped_count: int = 0,
) -> ContainerLogBatchEntry:
    return ContainerLogBatchEntry(
        sequence=sequence,
        stream=stream,
        message=message,
        timestamp=datetime(2026, 7, 16, 18, 0, sequence, tzinfo=UTC),
        kind=kind,
        dropped_count=dropped_count,
    )
