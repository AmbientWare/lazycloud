from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from database.tables.container_rollouts import ContainerRolloutDrainTable
from database.tables.orchestration import ContainerTable
from shared.containers import LIVE_CONTAINER_STATUSES, ContainerRecord, ContainerStatus
from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session


@dataclass(slots=True)
class ContainerRolloutRepository:
    session: Session

    def record_workload_ready(self, container_id: str, *, now: datetime) -> None:
        self.session.execute(
            update(ContainerTable)
            .where(
                ContainerTable.id == container_id,
                ContainerTable.workload_ready_at.is_(None),
            )
            .values(workload_ready_at=now)
        )

    def ready_container_ids(self, container_ids: Sequence[str]) -> set[str]:
        if not container_ids:
            return set()
        return set(
            self.session.scalars(
                select(ContainerTable.id).where(
                    ContainerTable.id.in_(container_ids),
                    ContainerTable.workload_ready_at.is_not(None),
                )
            )
        )

    def accepting_work(self, container_id: str, *, stub_id: str) -> bool:
        container = self.session.scalar(
            select(ContainerTable).where(ContainerTable.id == container_id).with_for_update()
        )
        if (
            container is None
            or container.stub_id != stub_id
            or container.status not in {item.value for item in LIVE_CONTAINER_STATUSES}
        ):
            return False
        return not bool(
            self.session.scalar(
                select(
                    exists().where(
                        ContainerRolloutDrainTable.container_id == container_id,
                        ContainerRolloutDrainTable.admission_closed_at.is_not(None),
                    )
                )
            )
        )

    def prepare(self, container: ContainerRecord, *, serving_floor: int, now: datetime) -> None:
        if container.stub_id is None:
            return
        row = self.session.scalar(
            select(ContainerTable).where(ContainerTable.id == container.id).with_for_update()
        )
        if row is None or row.status not in {item.value for item in LIVE_CONTAINER_STATUSES}:
            return
        if self.session.get(ContainerRolloutDrainTable, container.id) is None:
            self.session.add(
                ContainerRolloutDrainTable(
                    container_id=container.id,
                    stub_id=container.stub_id,
                    serving_floor=serving_floor,
                    requested_at=now,
                )
            )
            self.session.flush()

    def close_admission(self, container_id: str, *, now: datetime) -> bool:
        self.session.scalar(
            select(ContainerTable).where(ContainerTable.id == container_id).with_for_update()
        )
        row = self.session.get(ContainerRolloutDrainTable, container_id)
        if row is None:
            return False
        if row.admission_closed_at is None:
            row.admission_closed_at = now
            self.session.flush()
        return True

    def admission_closed_at(self, container_id: str) -> datetime | None:
        row = self.session.get(ContainerRolloutDrainTable, container_id)
        return row.admission_closed_at if row is not None else None

    def draining_ids(self, container_ids: Sequence[str]) -> set[str]:
        if not container_ids:
            return set()
        return set(
            self.session.scalars(
                select(ContainerRolloutDrainTable.container_id).where(
                    ContainerRolloutDrainTable.container_id.in_(container_ids)
                )
            )
        )

    def serving_floor(self, stub_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.max(ContainerRolloutDrainTable.serving_floor))
                .join(ContainerTable, ContainerTable.id == ContainerRolloutDrainTable.container_id)
                .where(
                    ContainerRolloutDrainTable.stub_id == stub_id,
                    ContainerTable.status.in_([item.value for item in LIVE_CONTAINER_STATUSES]),
                )
            )
            or 0
        )

    def closed_for_stub(self, stub_id: str) -> set[str]:
        return set(
            self.session.scalars(
                select(ContainerRolloutDrainTable.container_id).where(
                    ContainerRolloutDrainTable.stub_id == stub_id,
                    ContainerRolloutDrainTable.admission_closed_at.is_not(None),
                )
            )
        )

    def serving_containers(self, stub_id: str) -> list[ContainerRecord]:
        return [
            ContainerRecord.model_validate(row.payload)
            for row in self.session.scalars(
                select(ContainerTable).where(
                    ContainerTable.stub_id == stub_id,
                    ContainerTable.status == ContainerStatus.Running.value,
                    ~exists().where(ContainerRolloutDrainTable.container_id == ContainerTable.id),
                )
            )
        ]
