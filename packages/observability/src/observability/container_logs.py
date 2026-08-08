from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from database.repositories.apps import AppRepository, StubRepository
from database.repositories.execution import TaskRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from database.types import DatabaseSession
from shared.containers import ContainerRecord
from shared.errors import ConflictError, NotFoundError
from shared.realtime.contracts import EventRecordType, create_cloud_event_record

from observability.context import ObservabilityContext
from observability.stream_state import RedisEventStreamRepository


class ContainerLogIngestionEntry(Protocol):
    @property
    def sequence(self) -> int: ...

    @property
    def stream(self) -> str: ...

    @property
    def message(self) -> str: ...

    @property
    def timestamp(self) -> datetime: ...

    @property
    def kind(self) -> str: ...

    @property
    def dropped_count(self) -> int: ...


class ContainerLogWorkerAssignmentError(ConflictError):
    pass


class ContainerLogMachineAssignmentError(ConflictError):
    pass


@dataclass(frozen=True, slots=True)
class ContainerLogRuntimeAttribution:
    worker_id: str
    machine_id: str = ""

    def __post_init__(self) -> None:
        worker_id = self.worker_id.strip()
        if not worker_id:
            raise ValueError("container log runtime worker id must not be empty")
        object.__setattr__(self, "worker_id", worker_id)
        object.__setattr__(self, "machine_id", self.machine_id.strip())


@dataclass(frozen=True, slots=True)
class ContainerLogIngestionResult:
    accepted_through: int
    appended_count: int


@dataclass(frozen=True, slots=True)
class _ContainerLogOwnership:
    container: ContainerRecord
    task_id: str


@dataclass(slots=True)
class ContainerLogIngestionService:
    context: ObservabilityContext
    streams: RedisEventStreamRepository

    def append_batch(
        self,
        *,
        container_id: str,
        capture_id: str,
        entries: Sequence[ContainerLogIngestionEntry],
        expected_worker_id: str | None = None,
    ) -> ContainerLogIngestionResult:
        if not entries:
            raise ValueError("container log batch must not be empty")
        with self.context.database.session() as session:
            ownership = self._load_ownership(session, container_id)
            container = ownership.container
            worker_id = self._validated_durable_assignment(session, container)
            if expected_worker_id is not None and worker_id != expected_worker_id:
                raise ContainerLogWorkerAssignmentError(
                    "container log ingestion does not match the assigned worker"
                )
        return self._append_owned_batch(
            ownership=ownership,
            attribution=ContainerLogRuntimeAttribution(
                worker_id=worker_id,
                machine_id=container.machine_id or "",
            ),
            capture_id=capture_id,
            entries=entries,
        )

    def append_runtime_batch(
        self,
        *,
        container_id: str,
        capture_id: str,
        entries: Sequence[ContainerLogIngestionEntry],
        attribution: ContainerLogRuntimeAttribution,
    ) -> ContainerLogIngestionResult:
        if not entries:
            raise ValueError("container log batch must not be empty")
        with self.context.database.session() as session:
            ownership = self._load_ownership(session, container_id)
            resolved_attribution = self._resolve_runtime_attribution(
                session,
                ownership.container,
                attribution,
            )
        return self._append_owned_batch(
            ownership=ownership,
            attribution=resolved_attribution,
            capture_id=capture_id,
            entries=entries,
        )

    def _append_owned_batch(
        self,
        *,
        ownership: _ContainerLogOwnership,
        attribution: ContainerLogRuntimeAttribution,
        capture_id: str,
        entries: Sequence[ContainerLogIngestionEntry],
    ) -> ContainerLogIngestionResult:
        container = ownership.container
        stored_at_ns = time.time_ns()
        events = tuple(
            create_cloud_event_record(
                EventRecordType.ContainerLog,
                {
                    "workspace_id": container.workspace_id,
                    "stub_id": container.stub_id or "",
                    "app_id": container.app_id or "",
                    "task_id": ownership.task_id,
                    "container_id": container.id,
                    "machine_id": attribution.machine_id,
                    "worker_id": attribution.worker_id,
                    "message": entry.message,
                    "stream": str(entry.stream),
                    "timestamp": entry.timestamp,
                    "stored_at_ns": stored_at_ns,
                    "capture_id": capture_id,
                    "source_sequence": entry.sequence,
                    "entry_kind": str(entry.kind),
                    "dropped_count": entry.dropped_count,
                },
                event_id=_container_log_event_id(
                    container.id,
                    capture_id,
                    entry.sequence,
                ),
            )
            for entry in entries
        )
        result = self.streams.append_container_log_batch(
            container_id=container.id,
            capture_id=capture_id,
            first_sequence=entries[0].sequence,
            events=events,
        )
        if result.sequence_gap:
            raise ConflictError(
                f"container log sequence gap: expected sequence {result.accepted_through + 1}"
            )
        return ContainerLogIngestionResult(
            accepted_through=result.accepted_through,
            appended_count=result.appended_count,
        )

    @classmethod
    def _load_ownership(
        cls,
        session: DatabaseSession,
        container_id: str,
    ) -> _ContainerLogOwnership:
        container = ContainerRepository(session).get_across_workspaces(container_id)
        if container is None:
            raise NotFoundError(f"container not found: {container_id}")
        if WorkspaceRepository(session).get(container.workspace_id) is None:
            raise ConflictError("container log workspace metadata is unavailable")
        if container.app_id:
            app = AppRepository(session).get(container.app_id, workspace_id=container.workspace_id)
            if app is None:
                raise ConflictError("container log app metadata does not match container")
        if container.stub_id:
            stub = StubRepository(session).get(
                container.stub_id,
                workspace_id=container.workspace_id,
            )
            if stub is None:
                raise ConflictError("container log stub metadata does not match container")
            if stub.app_id != container.app_id:
                raise ConflictError("container log stub app does not match container")
        return _ContainerLogOwnership(
            container=container,
            task_id=cls._validated_task_id(session, container),
        )

    @staticmethod
    def _validated_durable_assignment(
        session: DatabaseSession,
        container: ContainerRecord,
    ) -> str:
        if not container.worker_id:
            raise ConflictError("container log ingestion requires a durable worker assignment")
        workers = WorkerRepository(session)
        worker = workers.get_across_workspaces(container.worker_id)
        # The container names its own worker, which is what ties the batch to it. The
        # worker's enrolling workspace is not part of that: a private worker serves
        # every workspace its account owns, so comparing the two dropped the logs of
        # any container placed on its owner's other workspace.
        if worker is None:
            raise ConflictError("container log worker metadata does not match container")
        if worker.machine_id != container.machine_id:
            raise ConflictError("container log machine metadata does not match worker")
        if (
            container.machine_id
            and MachineRepository(session).get_across_workspaces(container.machine_id) is None
        ):
            raise ConflictError("container log machine metadata is unavailable")
        return container.worker_id

    @classmethod
    def _resolve_runtime_attribution(
        cls,
        session: DatabaseSession,
        container: ContainerRecord,
        attribution: ContainerLogRuntimeAttribution,
    ) -> ContainerLogRuntimeAttribution:
        worker_id = attribution.worker_id
        machine_id = attribution.machine_id
        if container.runtime_worker_id and container.runtime_worker_id != worker_id:
            raise ContainerLogWorkerAssignmentError(
                "container log runtime worker does not match the execution assignment"
            )
        if container.runtime_machine_id and container.runtime_machine_id != machine_id:
            raise ContainerLogMachineAssignmentError(
                "container log runtime machine does not match the execution assignment"
            )
        if container.worker_id:
            durable_worker_id = cls._validated_durable_assignment(session, container)
            if container.runtime_worker_id and durable_worker_id != worker_id:
                raise ContainerLogWorkerAssignmentError(
                    "container log runtime worker does not match the durable assignment"
                )
            if not container.runtime_worker_id:
                if durable_worker_id != worker_id:
                    raise ContainerLogWorkerAssignmentError(
                        "container log runtime worker does not match the durable assignment"
                    )
                worker_id = durable_worker_id
        elif (
            container.machine_id
            and MachineRepository(session).get_across_workspaces(container.machine_id) is None
        ):
            raise ConflictError("container log machine metadata is unavailable")
        if container.machine_id:
            if container.runtime_machine_id and container.machine_id != machine_id:
                raise ContainerLogMachineAssignmentError(
                    "container log runtime machine does not match the durable assignment"
                )
            if not container.runtime_machine_id:
                if container.machine_id != machine_id:
                    raise ContainerLogMachineAssignmentError(
                        "container log runtime machine does not match the durable assignment"
                    )
                machine_id = container.machine_id
        return ContainerLogRuntimeAttribution(
            worker_id=worker_id,
            machine_id=machine_id,
        )

    @staticmethod
    def _validated_task_id(
        session: DatabaseSession,
        container: ContainerRecord,
    ) -> str:
        if not container.task_id:
            return ""
        task = TaskRepository(session).get_across_workspaces(container.task_id)
        if task is None:
            raise ConflictError("container log task metadata is unavailable")
        if task.workspace_id and task.workspace_id != container.workspace_id:
            raise ConflictError("container log task workspace does not match container")
        if task.stub_id and task.stub_id != container.stub_id:
            raise ConflictError("container log task stub does not match container")
        if task.app_id and task.app_id != container.app_id:
            raise ConflictError("container log task app does not match container")
        if task.container_id and task.container_id != container.id:
            raise ConflictError("container log task does not match container")
        return task.id


def _container_log_event_id(container_id: str, capture_id: str, sequence: int) -> str:
    digest = hashlib.sha256(f"{container_id}\0{capture_id}\0{sequence}".encode()).hexdigest()
    return f"container-log-{digest}"
