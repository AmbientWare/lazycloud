from __future__ import annotations

from observability.event_summary import (
    ContainerEventsBatchRequest,
    ContainerEventsBatchTarget,
    normalize_batch_targets,
    normalize_event_types,
)
from observability.events import EventService
from shared.realtime.contracts import EventRecordType


def test_event_target_and_type_normalization() -> None:
    request = ContainerEventsBatchRequest(
        targets=[
            ContainerEventsBatchTarget(container_id=" ctr-1 ", stub_id=" stub-1 "),
            ContainerEventsBatchTarget(container_id="ctr-1", stub_id="stub-1"),
            ContainerEventsBatchTarget(task_id=" task-1 "),
        ],
        container_ids=["ctr-2", ""],
        task_ids=["task-1", "task-2"],
    )

    targets = normalize_batch_targets(request)
    assert len(targets) == 4
    assert targets[0].container_id == "ctr-1"
    assert targets[0].stub_id == "stub-1"
    assert targets[1].task_id == "task-1"
    assert normalize_event_types([" container.* ", "", "container.started", "container.*"]) == (
        "container.*",
        "container.started",
    )


def _emit_lifecycle(
    events: EventService,
    workspace_id: str,
    container_id: str,
    event_id: str,
    duration_ms: int,
    task_id: str,
) -> None:
    stream_events = events.stream_events
    assert stream_events is not None
    stream_events.append_event(
        EventRecordType.ContainerLifecycle,
        {
            "workspace_id": workspace_id,
            "stub_id": "stub-1",
            "container_id": container_id,
            "task_id": task_id,
            "id": event_id,
            "duration_ms": duration_ms,
            "start_time": "2026-06-20T10:00:00+00:00",
            "end_time": "2026-06-20T10:00:01+00:00",
        },
    )
