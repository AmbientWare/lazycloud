from __future__ import annotations

from dataclasses import dataclass, field

from gateway.payloads import CONTAINER_OUTPUT_EVENT_LIMIT, container_output
from shared.containers import ContainerRecord
from shared.events import Event
from shared.logs import LogEntry


@dataclass
class FakeTaskService:
    entries: list[LogEntry] = field(default_factory=list)

    def logs(self, task_id: str) -> list[LogEntry]:
        return [entry for entry in self.entries if entry.task_id == task_id]


@dataclass
class FakeEventService:
    events: list[Event]
    calls: list[tuple[str, str, int | None]] = field(default_factory=list)

    def list_for_resource(
        self,
        *,
        resource_type: str,
        resource_id: str,
        limit: int | None = None,
    ) -> list[Event]:
        self.calls.append((resource_type, resource_id, limit))
        return [
            event
            for event in self.events
            if event.resource_type == resource_type and event.resource_id == resource_id
        ][:limit]


@dataclass
class FakeGatewayServices:
    tasks: FakeTaskService
    events: FakeEventService


def test_container_output_uses_bounded_container_event_lookup() -> None:
    container = ContainerRecord(
        id="ctr-serve",
        name="endpoint-health",
        image="img",
        command=["python"],
        workspace_id="workspace",
    )
    events = FakeEventService(
        [
            Event(
                id="evt-other",
                action="container.output",
                resource_type="container",
                resource_id="other",
                message="other",
                data={"stdout": "wrong"},
            ),
            Event(
                id="evt-target",
                action="container.output",
                resource_type="container",
                resource_id=container.id,
                message="target",
                data={"stdout": "serve ready\n"},
            ),
        ]
    )
    services = FakeGatewayServices(tasks=FakeTaskService(), events=events)

    assert container_output(services, container) == "serve ready\n"
    assert events.calls == [("container", container.id, CONTAINER_OUTPUT_EVENT_LIMIT)]
