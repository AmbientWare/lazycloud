from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from database.tables.capacity_activations import CapacityActivationTable
from database.tables.compute import ComputeProviderInstanceTable, ComputeUnitTable
from shared.capacity_lifecycle import CapacityActivationKind, CapacitySleepOutcome
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class CapacityActivationSummary:
    instance_type: str
    gpu_type: str
    kind: CapacityActivationKind
    sleep_outcome: CapacitySleepOutcome
    attempts: int
    ready: int
    prepared: int
    failed: int
    p95_ready_seconds: float | None
    p95_provider_seconds: float | None


@dataclass(slots=True)
class CapacityActivationRepository:
    session: Session

    def observe(
        self,
        instance_record_id: str,
        *,
        requested_at: datetime,
        kind: CapacityActivationKind,
        provider_running_at: datetime | None,
        observed_at: datetime,
    ) -> None:
        table = CapacityActivationTable
        self.session.execute(
            update(table)
            .where(
                table.instance_record_id == instance_record_id,
                table.requested_at < requested_at,
                table.ready_at.is_(None),
                table.prepared_at.is_(None),
                table.failed_at.is_(None),
            )
            .values(failed_at=observed_at)
        )
        statement = insert(table).values(
            instance_record_id=instance_record_id,
            requested_at=requested_at,
            kind=kind.value,
            sleep_outcome=CapacitySleepOutcome.Unknown.value,
            provider_running_at=provider_running_at,
        )
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[table.instance_record_id, table.requested_at],
                set_={
                    "provider_running_at": func.coalesce(
                        table.provider_running_at, statement.excluded.provider_running_at
                    )
                },
            )
        )

    def authorize(
        self, instance_record_id: str, *, at: datetime, outcome: CapacitySleepOutcome
    ) -> None:
        table = CapacityActivationTable
        self.session.execute(
            update(table)
            .where(
                table.instance_record_id == instance_record_id,
                table.ready_at.is_(None),
                table.prepared_at.is_(None),
                table.failed_at.is_(None),
                table.authorized_at.is_(None),
            )
            .values(authorized_at=at, sleep_outcome=outcome.value)
        )

    def record_ready(self, machine_ids: Collection[str], *, at: datetime) -> None:
        if not machine_ids:
            return
        table = CapacityActivationTable
        self.session.execute(
            update(table)
            .where(
                table.instance_record_id.in_(
                    select(ComputeProviderInstanceTable.id).where(
                        ComputeProviderInstanceTable.machine_id.in_(machine_ids),
                        ComputeProviderInstanceTable.status.in_(("active", "resuming")),
                    )
                ),
                table.ready_at.is_(None),
                table.prepared_at.is_(None),
                table.failed_at.is_(None),
                or_(
                    table.kind == CapacityActivationKind.Provision.value,
                    table.authorized_at.is_not(None),
                ),
            )
            .values(ready_at=at)
        )

    def record_failed(self, instance_record_id: str, *, at: datetime) -> None:
        table = CapacityActivationTable
        self.session.execute(
            update(table)
            .where(
                table.instance_record_id == instance_record_id,
                table.ready_at.is_(None),
                table.prepared_at.is_(None),
                table.failed_at.is_(None),
            )
            .values(failed_at=at)
        )

    def record_prepared(self, instance_record_id: str, *, at: datetime) -> None:
        table = CapacityActivationTable
        self.session.execute(
            update(table)
            .where(
                table.instance_record_id == instance_record_id,
                table.ready_at.is_(None),
                table.prepared_at.is_(None),
                table.failed_at.is_(None),
            )
            .values(prepared_at=at)
        )

    def summarize(self, *, since: datetime) -> tuple[CapacityActivationSummary, ...]:
        table = CapacityActivationTable
        instance = ComputeProviderInstanceTable
        rows = self.session.execute(
            select(
                instance.instance_type,
                instance.gpu,
                table.kind,
                table.sleep_outcome,
                func.count(),
                func.count().filter(table.ready_at.is_not(None)),
                func.count().filter(table.prepared_at.is_not(None)),
                func.count().filter(table.failed_at.is_not(None)),
                func.percentile_cont(0.95).within_group(
                    func.extract("epoch", table.ready_at - table.requested_at)
                ),
                func.percentile_cont(0.95).within_group(
                    func.extract("epoch", table.provider_running_at - table.requested_at)
                ),
            )
            .join(instance, instance.id == table.instance_record_id)
            .join(ComputeUnitTable, ComputeUnitTable.id == instance.pool_id)
            .where(table.requested_at >= since, ComputeUnitTable.platform_fleet.is_(True))
            .group_by(instance.instance_type, instance.gpu, table.kind, table.sleep_outcome)
        ).tuples()
        return tuple(
            CapacityActivationSummary(
                instance_type=instance_type or "",
                gpu_type=gpu or "",
                kind=CapacityActivationKind(kind),
                sleep_outcome=CapacitySleepOutcome(outcome),
                attempts=attempts,
                ready=ready,
                prepared=prepared,
                failed=failed,
                p95_ready_seconds=float(ready_seconds) if ready_seconds is not None else None,
                p95_provider_seconds=float(provider_seconds)
                if provider_seconds is not None
                else None,
            )
            for (
                instance_type,
                gpu,
                kind,
                outcome,
                attempts,
                ready,
                prepared,
                failed,
                ready_seconds,
                provider_seconds,
            ) in rows
        )
