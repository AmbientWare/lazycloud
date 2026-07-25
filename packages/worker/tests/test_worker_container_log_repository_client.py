from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue, ValidationError
from worker.repository_payloads import (
    AppendContainerLogsRequest,
    AppendContainerLogsResponse,
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


@dataclass(slots=True)
class _Transport:
    posts: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)

    def set_bearer_token(self, token: str) -> None:
        _ = token

    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> dict[str, JsonValue]:
        self.posts.append((path, dict(payload)))
        return AppendContainerLogsResponse(
            accepted_through=3,
            appended_count=4,
        ).model_dump(mode="json")

    def stream(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
    ) -> Iterator[dict[str, JsonValue]]:
        _ = path, payload
        return iter(())
