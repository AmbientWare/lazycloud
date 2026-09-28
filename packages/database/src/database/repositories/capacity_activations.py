from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from database.tables.capacity_activations import CapacityActivationTable
from database.tables.compute import ComputeProviderInstanceTable, ComputeUnitTable
from shared.capacity_lifecycle import CapacityActivationKind, CapacityRestoreOutcome
from shared.errors import ConflictError
from shared.timestamps import to_utc
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased


@dataclass(frozen=True, slots=True)
class CapacityActivationSummary:
    provider: str
    region: str
    architecture: str
    instance_type: str
    gpu_type: str
    kind: CapacityActivationKind
    restore_outcome: CapacityRestoreOutcome
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
        sleep_attempt_id: str | None = None,
    ) -> None:
        table = CapacityActivationTable
        instance = self.session.scalar(
            select(ComputeProviderInstanceTable)
            .where(ComputeProviderInstanceTable.id == instance_record_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if instance is None:
            raise ConflictError("activation requires an existing provider instance")
        latest = self.session.execute(
            select(
                table.requested_at,
                table.sleep_attempt_id,
                func.coalesce(table.requested_at, table.observed_at).label("started_at"),
            )
            .where(table.instance_record_id == instance_record_id)
            .order_by(func.coalesce(table.requested_at, table.observed_at).desc())
            .limit(1)
        ).one_or_none()
        if latest is not None and to_utc(latest.started_at) > requested_at:
            return
        existing_attempt = (
            latest is not None
            and latest.requested_at is not None
            and to_utc(latest.requested_at) == requested_at
            and latest.sleep_attempt_id == sleep_attempt_id
        )
        if (
            not existing_attempt
            and sleep_attempt_id is not None
            and sleep_attempt_id != instance.current_sleep_attempt_id
        ):
            raise ConflictError("activation does not name the current sleep attempt")
        self.session.execute(
            update(table)
            .where(
                table.instance_record_id == instance_record_id,
                func.coalesce(table.requested_at, table.observed_at) < requested_at,
                table.ready_at.is_(None),
                table.prepared_at.is_(None),
                table.failed_at.is_(None),
            )
            .values(failed_at=observed_at)
        )
        statement = insert(table).values(
            instance_record_id=instance_record_id,
            requested_at=requested_at,
            observed_at=observed_at,
            kind=kind.value,
            sleep_attempt_id=sleep_attempt_id,
            restore_outcome=CapacityRestoreOutcome.Unknown.value,
            provider_running_at=provider_running_at,
        )
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[table.instance_record_id, table.requested_at],
                set_={
                    "provider_running_at": func.coalesce(
                        table.provider_running_at, statement.excluded.provider_running_at
                    ),
                    "sleep_attempt_id": func.coalesce(
                        table.sleep_attempt_id, statement.excluded.sleep_attempt_id
                    ),
                },
            )
        )

    def observe_external(
        self,
        instance_record_id: str,
        *,
        observed_at: datetime,
        kind: CapacityActivationKind,
        provider_running_at: datetime | None,
        sleep_attempt_id: str,
    ) -> None:
        table = CapacityActivationTable
        instance = self.session.scalar(
            select(ComputeProviderInstanceTable)
            .where(ComputeProviderInstanceTable.id == instance_record_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if instance is None:
            raise ConflictError("external activation requires an existing provider instance")
        existing = self.session.scalar(
            select(table.id).where(
                table.instance_record_id == instance_record_id,
                table.sleep_attempt_id == sleep_attempt_id,
                table.requested_at.is_(None),
            )
        )
        if existing is not None:
            self.session.execute(
                update(table)
                .where(table.id == existing)
                .values(
                    provider_running_at=func.coalesce(
                        table.provider_running_at, provider_running_at
                    )
                )
            )
            return
        if instance.current_sleep_attempt_id != sleep_attempt_id:
            raise ConflictError("external activation does not name the current sleep attempt")
        latest_at = self.session.scalar(
            select(func.max(func.coalesce(table.requested_at, table.observed_at))).where(
                table.instance_record_id == instance_record_id
            )
        )
        if latest_at is not None and to_utc(latest_at) > observed_at:
            return
        self.session.execute(
            update(table)
            .where(
                table.instance_record_id == instance_record_id,
                table.ready_at.is_(None),
                table.prepared_at.is_(None),
                table.failed_at.is_(None),
            )
            .values(failed_at=observed_at)
        )
        self.session.execute(
            insert(table).values(
                instance_record_id=instance_record_id,
                requested_at=None,
                observed_at=observed_at,
                kind=kind.value,
                sleep_attempt_id=sleep_attempt_id,
                restore_outcome=CapacityRestoreOutcome.Unknown.value,
                provider_running_at=provider_running_at,
            )
        )

    def authorize(self, instance_record_id: str, *, at: datetime) -> None:
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
            .values(authorized_at=at)
        )

    def record_restore(
        self,
        instance_record_id: str,
        *,
        sleep_attempt_id: str,
        outcome: CapacityRestoreOutcome,
        at: datetime,
    ) -> bool:
        if outcome is CapacityRestoreOutcome.Unknown:
            return False
        table = CapacityActivationTable
        history = aliased(CapacityActivationTable)
        recorded = self.session.scalar(
            update(table)
            .where(
                table.instance_record_id == instance_record_id,
                table.sleep_attempt_id == sleep_attempt_id,
                func.coalesce(table.requested_at, table.observed_at) <= at,
                func.coalesce(table.requested_at, table.observed_at)
                == select(func.max(func.coalesce(history.requested_at, history.observed_at)))
                .where(history.instance_record_id == instance_record_id)
                .scalar_subquery(),
                table.failed_at.is_(None),
                table.restore_outcome.in_((CapacityRestoreOutcome.Unknown.value, outcome.value)),
                table.instance_record_id.in_(
                    select(ComputeProviderInstanceTable.id).where(
                        ComputeProviderInstanceTable.current_sleep_attempt_id == sleep_attempt_id
                    )
                ),
            )
            .values(
                restore_outcome=outcome.value,
                restore_observed_at=func.coalesce(table.restore_observed_at, at),
            )
            .returning(table.instance_record_id)
        )
        return recorded is not None

    def record_ready(self, machine_ids: Collection[str], *, at: datetime) -> None:
        if not machine_ids:
            return
        table = CapacityActivationTable
        self.session.execute(
            update(table)
            .where(
                table.instance_record_id.in_(
                    select(ComputeProviderInstanceTable.id)
                    .where(
                        ComputeProviderInstanceTable.machine_id.in_(machine_ids),
                        or_(
                            ComputeProviderInstanceTable.status.in_(("active", "resuming")),
                            and_(
                                ComputeProviderInstanceTable.status == "pending",
                                table.kind == CapacityActivationKind.Provision.value,
                            ),
                        ),
                    )
                    .correlate(table)
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
                ComputeUnitTable.provider,
                instance.region,
                instance.architecture,
                instance.instance_type,
                instance.gpu,
                table.kind,
                table.restore_outcome,
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
            .group_by(
                ComputeUnitTable.provider,
                instance.region,
                instance.architecture,
                instance.instance_type,
                instance.gpu,
                table.kind,
                table.restore_outcome,
            )
        ).tuples()
        return tuple(
            CapacityActivationSummary(
                provider=provider,
                region=region,
                architecture=architecture,
                instance_type=instance_type or "",
                gpu_type=gpu or "",
                kind=CapacityActivationKind(kind),
                restore_outcome=CapacityRestoreOutcome(outcome),
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
                provider,
                region,
                architecture,
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
