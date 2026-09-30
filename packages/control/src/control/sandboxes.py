from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.records.apps import StubKind
from database.repositories.orchestration import ContainerRepository
from database.repositories.sandboxes import SandboxRepository
from shared.containers import ContainerStatus
from shared.errors import InvalidInputError
from shared.http.pods import (
    SandboxCreatedBucket,
    SandboxDashboardStatus,
    SandboxListResponse,
    SandboxRow,
    SandboxStatsResponse,
    SandboxTimeline,
)
from shared.timestamps import utc_now

from control.context import ControlContext
from control.stubs import StubService


def _sandbox_status(
    status: str | None, container_status: ContainerStatus | None
) -> SandboxDashboardStatus:
    value = (
        container_status.value if container_status is not None else (status or "stopped").lower()
    )
    try:
        return SandboxDashboardStatus(value)
    except ValueError:
        return SandboxDashboardStatus.Stopped


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    milliseconds = int((end - start).total_seconds() * 1000)
    return max(milliseconds, 0)


@dataclass(slots=True)
class SandboxQueryService:
    context: ControlContext
    stubs: StubService

    def list_sandbox_rows(
        self, *, workspace: str = "default", app_id: str | None = None, limit: int = 50
    ) -> SandboxListResponse:
        with self.context.database.session() as session:
            workspace_record = self.context.workspace(session, workspace)
            observations = SandboxRepository(session).list(
                workspace_id=workspace_record.id, app_id=app_id, limit=max(min(limit, 200), 1)
            )
        return SandboxListResponse(
            data=tuple(
                SandboxRow(
                    id=row.id,
                    stub_id=row.id,
                    name=row.name,
                    created_at=row.created_at,
                    status=_sandbox_status(row.configured_status, row.container_status),
                    gpu=row.gpu or [],
                    container_id=row.container_id,
                    time_to_started_ms=_duration_ms(row.created_at, row.started_at),
                    lifetime_ms=_duration_ms(row.started_at, row.finished_at),
                )
                for row in observations
            )
        )

    def sandbox_stats(
        self, *, workspace: str = "default", app_id: str | None = None
    ) -> SandboxStatsResponse:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            rows = SandboxRepository(session).statistics(
                workspace_id=workspace_id, app_id=app_id, now=utc_now()
            )
        status_counts = {status: 0 for status in SandboxDashboardStatus}
        buckets: dict[datetime, int] = {}
        for row in rows:
            status_counts[_sandbox_status(row.configured_status, row.container_status)] += row.count
            buckets[row.day] = buckets.get(row.day, 0) + row.count
        return SandboxStatsResponse(
            concurrent=sum(
                status_counts[status]
                for status in (
                    SandboxDashboardStatus.Pending,
                    SandboxDashboardStatus.Running,
                    SandboxDashboardStatus.Stopping,
                )
            ),
            total_created=sum(status_counts.values()),
            rate_per_second=sum(row.recent_count for row in rows) / 86400,
            status_counts=status_counts,
            created_buckets=tuple(
                SandboxCreatedBucket(timestamp=day, count=count)
                for day, count in sorted(buckets.items())
            ),
        )

    def sandbox_timeline(
        self, stub_id_or_name: str, *, workspace: str = "default", container_id: str | None = None
    ) -> SandboxTimeline:
        stub = self.stubs.get_stub(stub_id_or_name, workspace=workspace)
        if stub.kind is not StubKind.Sandbox:
            raise InvalidInputError(f"stub is not a sandbox: {stub_id_or_name}")
        with self.context.database.session() as session:
            repository = ContainerRepository(session)
            container = (
                repository.get_for_stub(
                    container_id, workspace_id=stub.workspace_id, stub_id=stub.id
                )
                if container_id
                else repository.latest_for_stubs(
                    workspace_id=stub.workspace_id, stub_ids=(stub.id,)
                ).get(stub.id)
            )
        created_at = stub.created_at
        started_at = container.started_at if container else None
        ended_at = container.finished_at if container else None
        return SandboxTimeline(
            container_id=container.id if container else container_id,
            status=_sandbox_status(stub.config.status, container.status if container else None),
            created_at=created_at,
            started_at=started_at,
            ended_at=ended_at,
            startup_ms=_duration_ms(created_at, started_at),
            runtime_ms=_duration_ms(started_at, ended_at),
        )
