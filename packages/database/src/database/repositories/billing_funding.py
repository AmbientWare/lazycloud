from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.billing_credits import (
    BillingCreditAllocationTable,
    BillingCreditSettlementTable,
)
from database.tables.billing_funding import (
    BillingFundingAllocationTable,
    BillingFundingHoldTable,
    BillingFundingWindowTable,
)
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.compute import ComputeProviderInstanceTable
from database.tables.identity import WorkspaceMemberTable
from database.tables.observability import UsageRecordTable
from database.tables.orchestration import ContainerTable
from shared.billing_quotes import BILLED_METRICS, BilledDimension, ContainerShape
from shared.containers import LIVE_CONTAINER_STATUSES
from shared.errors import ConflictError, NotFoundError, PaymentRequiredError
from shared.funding import FundingPermit
from shared.identity import WorkspaceRole
from shared.timestamps import to_utc
from shared.usage import UsageBillingOwner, UsageRecord
from sqlalchemy import String, cast, delete, func, or_, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class DestroyedWorkerEvidence:
    machine_id: str
    provider_instance_id: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class FundingHold:
    container_id: str
    workspace_id: str
    user_id: str
    worker_id: str
    shape: ContainerShape
    revision: int
    authorized_at: datetime | None
    valid_until: datetime | None
    metered_through: datetime | None
    terminal_at: datetime | None
    cancelled_at: datetime | None
    loss_resolved_at: datetime | None

    def permit(self) -> FundingPermit:
        if self.valid_until is None:
            raise ConflictError("the container has no funded runtime permit")
        return FundingPermit(
            container_id=self.container_id,
            revision=self.revision,
            valid_until=self.valid_until,
        )


@dataclass(frozen=True, slots=True)
class BillingFundingRepository:
    session: Session

    def recovery_accounts(
        self, *, after_user_id: str | None, expired_before: datetime, limit: int
    ) -> tuple[str, ...]:
        statement = select(BillingFundingHoldTable.user_id).where(
            BillingFundingHoldTable.valid_until <= expired_before,
            BillingFundingHoldTable.loss_resolved_at.is_(None),
            BillingFundingHoldTable.cancelled_at.is_(None),
            or_(
                BillingFundingHoldTable.terminal_at.is_(None),
                BillingFundingHoldTable.metered_through.is_(None),
                BillingFundingHoldTable.metered_through < BillingFundingHoldTable.terminal_at,
            ),
        )
        if after_user_id is not None:
            statement = statement.where(BillingFundingHoldTable.user_id > after_user_id)
        return tuple(
            str(value)
            for value in self.session.scalars(
                statement.distinct().order_by(BillingFundingHoldTable.user_id).limit(limit)
            )
        )

    def require_machine_evidence_released(self, machine_ids: Sequence[str]) -> None:
        if not machine_ids:
            return
        unresolved = self.session.scalar(
            select(BillingFundingHoldTable.container_id)
            .join(ContainerTable, ContainerTable.id == BillingFundingHoldTable.container_id)
            .where(
                ContainerTable.payload["runtime_machine_id"].as_string().in_(machine_ids),
                BillingFundingHoldTable.authorized_at.is_not(None),
                BillingFundingHoldTable.cancelled_at.is_(None),
                BillingFundingHoldTable.loss_resolved_at.is_(None),
                or_(
                    BillingFundingHoldTable.terminal_at.is_(None),
                    BillingFundingHoldTable.metered_through.is_(None),
                    BillingFundingHoldTable.metered_through < BillingFundingHoldTable.terminal_at,
                ),
            )
            .limit(1)
        )
        if unresolved is not None:
            raise ConflictError("machine evidence is retained until funded execution is reconciled")

    def for_account(self, user_id: str) -> tuple[FundingHold, ...]:
        return tuple(
            _hold(row)
            for row in self.session.scalars(
                select(BillingFundingHoldTable).where(
                    BillingFundingHoldTable.user_id == user_id,
                    BillingFundingHoldTable.cancelled_at.is_(None),
                    BillingFundingHoldTable.loss_resolved_at.is_(None),
                    or_(
                        BillingFundingHoldTable.terminal_at.is_(None),
                        BillingFundingHoldTable.metered_through.is_(None),
                        BillingFundingHoldTable.metered_through
                        < BillingFundingHoldTable.terminal_at,
                    ),
                )
            )
        )

    def unfunded_live_container_ids(
        self, *, user_id: str, at: datetime, limit: int
    ) -> tuple[str, ...]:
        rows = self.session.scalars(
            select(ContainerTable.id)
            .join(
                WorkspaceMemberTable,
                WorkspaceMemberTable.workspace_id == ContainerTable.workspace_id,
            )
            .outerjoin(
                BillingFundingHoldTable,
                BillingFundingHoldTable.container_id == ContainerTable.id,
            )
            .where(
                WorkspaceMemberTable.user_id == user_id,
                WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
                ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
                or_(
                    BillingFundingHoldTable.container_id.is_(None),
                    BillingFundingHoldTable.cancelled_at.is_not(None),
                    BillingFundingHoldTable.valid_until <= at,
                ),
            )
            .order_by(ContainerTable.created_at, ContainerTable.id)
            .limit(limit)
        )
        return tuple(str(row) for row in rows)

    def get(self, container_id: str) -> FundingHold | None:
        row = self.session.get(BillingFundingHoldTable, container_id)
        return _hold(row) if row is not None else None

    def destroyed_worker_candidates(
        self, *, user_id: str, expired_before: datetime, limit: int
    ) -> tuple[FundingHold, ...]:
        return tuple(
            _hold(row)
            for row in self.session.scalars(
                select(BillingFundingHoldTable)
                .join(ContainerTable, ContainerTable.id == BillingFundingHoldTable.container_id)
                .join(
                    ComputeProviderInstanceTable,
                    cast(ComputeProviderInstanceTable.machine_id, String)
                    == ContainerTable.payload["runtime_machine_id"].as_string(),
                )
                .where(
                    BillingFundingHoldTable.user_id == user_id,
                    BillingFundingHoldTable.valid_until <= expired_before,
                    BillingFundingHoldTable.loss_resolved_at.is_(None),
                    BillingFundingHoldTable.cancelled_at.is_(None),
                    ComputeProviderInstanceTable.payload["metadata"][
                        "provider_storage_destroyed_at"
                    ]
                    .as_string()
                    .is_not(None),
                )
                .order_by(BillingFundingHoldTable.valid_until, BillingFundingHoldTable.container_id)
                .limit(limit)
            )
        )

    def destroyed_worker_evidence(self, hold: FundingHold) -> DestroyedWorkerEvidence | None:
        container = ContainerRepository(self.session).get(
            hold.container_id, workspace_id=hold.workspace_id
        )
        if (
            container is None
            or container.runtime_worker_id != hold.worker_id
            or not container.runtime_machine_id
        ):
            return None
        instance = self.session.scalar(
            select(ComputeProviderInstanceTable).where(
                ComputeProviderInstanceTable.machine_id == container.runtime_machine_id
            )
        )
        if instance is None:
            return None
        metadata = instance.payload.get("metadata")
        value = (
            metadata.get("provider_storage_destroyed_at") if isinstance(metadata, dict) else None
        )
        if not isinstance(value, str):
            return None
        try:
            observed_at = datetime.fromisoformat(value)
        except ValueError:
            return None
        if observed_at.tzinfo is None:
            return None
        return DestroyedWorkerEvidence(
            container.runtime_machine_id, str(instance.id), to_utc(observed_at)
        )

    def unsettled_compute_records(self, container_id: str) -> tuple[UsageRecord, ...]:
        return tuple(
            UsageRecord.model_validate(row.payload)
            for row in self.session.scalars(
                select(UsageRecordTable).where(
                    UsageRecordTable.resource_type == "container",
                    UsageRecordTable.resource_id == container_id,
                    UsageRecordTable.metric.in_(
                        [
                            metric.value
                            for metric, billed in BILLED_METRICS.items()
                            if billed.dimension is BilledDimension.ComputeRuntime
                        ]
                    ),
                    ~select(BillingCreditSettlementTable.usage_record_id)
                    .where(
                        BillingCreditSettlementTable.usage_record_id == UsageRecordTable.id,
                        BillingCreditSettlementTable.settled_at.is_not(None),
                    )
                    .exists(),
                )
            )
        )

    def resolve_destroyed_worker(
        self,
        *,
        container_id: str,
        evidence: DestroyedWorkerEvidence,
        at: datetime,
        exposure_nanos: int,
    ) -> None:
        row = self._row(container_id)
        if row.loss_resolved_at is not None:
            return
        if exposure_nanos < 0:
            raise ValueError("lost execution exposure cannot be negative")
        row.loss_resolved_at = at
        row.loss_machine_id = evidence.machine_id
        row.loss_provider_instance_id = evidence.provider_instance_id
        row.loss_evidence_at = evidence.observed_at
        row.loss_exposure_nanos = exposure_nanos
        self.release(container_id)

    def lock(self, container_id: str) -> FundingHold:
        row = self.session.get(BillingFundingHoldTable, container_id)
        if row is None:
            raise NotFoundError("the container has no funded reservation")
        BillingAccountRepository(self.session).get_by_user(row.user_id, for_update=True)
        self.session.refresh(row)
        return _hold(row)

    def create(
        self,
        *,
        container_id: str,
        workspace_id: str,
        user_id: str,
        shape: ContainerShape,
    ) -> FundingHold:
        row = BillingFundingHoldTable(
            container_id=container_id,
            workspace_id=workspace_id,
            user_id=user_id,
            worker_id="",
            revision=1,
            billing_owner=shape.billing_owner.value,
            rate_class=shape.rate_class,
            gpu_type=shape.gpu_type,
            cpu_millicores=shape.cpu_millicores,
            memory_mib=shape.memory_mib,
            gpu_count=shape.gpu_count,
        )
        self.session.add(row)
        self.session.flush()
        return _hold(row)

    def authorize(
        self,
        *,
        container_id: str,
        worker_id: str,
        shape: ContainerShape,
        authorized_at: datetime,
        valid_until: datetime,
    ) -> FundingPermit:
        row = self._row(container_id)
        row.worker_id = worker_id
        row.billing_owner = shape.billing_owner.value
        row.rate_class = shape.rate_class
        row.gpu_type = shape.gpu_type
        row.cpu_millicores = shape.cpu_millicores
        row.memory_mib = shape.memory_mib
        row.gpu_count = shape.gpu_count
        row.authorized_at = authorized_at
        row.valid_until = valid_until
        row.revision += 1
        self.session.flush()
        return _hold(row).permit()

    def renew(self, *, container_id: str, valid_until: datetime) -> FundingPermit:
        row = self._row(container_id)
        row.valid_until = valid_until
        row.revision += 1
        self.session.flush()
        return _hold(row).permit()

    def reserve_credit(
        self,
        *,
        container_id: str,
        amount_nanos: int,
        at: datetime,
        eligible_until: datetime,
    ) -> None:
        hold = self.lock(container_id)
        credits = BillingCreditRepository(self.session)
        if credits.debt_nanos(user_id=hold.user_id, at=at) > 0:
            raise PaymentRequiredError(
                "purchased credit has an unpaid balance after a refund or dispute"
            )
        lots = credits.spendable_lots(
            user_id=hold.user_id,
            at=at,
            dimension=BilledDimension.ComputeRuntime,
            container_id=container_id,
        )
        eligible = tuple(
            lot for lot in lots if lot.expires_at is None or lot.expires_at >= eligible_until
        )
        if sum(lot.amount_nanos for lot in eligible) < amount_nanos:
            raise PaymentRequiredError(
                "available credit does not cover this container's funded runtime window"
            )
        self.session.execute(
            delete(BillingFundingAllocationTable).where(
                BillingFundingAllocationTable.container_id == container_id,
            )
        )
        remaining = amount_nanos
        for lot in eligible:
            allocated = min(remaining, lot.amount_nanos)
            if allocated:
                self.session.add(
                    BillingFundingAllocationTable(
                        container_id=container_id,
                        credit_lot_id=lot.id,
                        amount_nanos=allocated,
                    )
                )
                remaining -= allocated
            if remaining == 0:
                break
        self.session.flush()

    def release(self, container_id: str) -> None:
        self.session.execute(
            delete(BillingFundingAllocationTable).where(
                BillingFundingAllocationTable.container_id == container_id,
            )
        )
        self.session.flush()

    def release_excess(self, *, container_id: str, required_nanos: int) -> None:
        allocations = self.session.scalars(
            select(BillingFundingAllocationTable)
            .where(
                BillingFundingAllocationTable.container_id == container_id,
            )
            .order_by(BillingFundingAllocationTable.credit_lot_id)
        ).all()
        release = max(0, sum(row.amount_nanos for row in allocations) - required_nanos)
        for row in allocations:
            if release == 0:
                break
            amount = min(release, row.amount_nanos)
            if amount == row.amount_nanos:
                self.session.delete(row)
            else:
                row.amount_nanos -= amount
            release -= amount
        self.session.flush()

    def cancel_pending(self, *, container_id: str, at: datetime) -> bool:
        hold = self.lock(container_id)
        if hold.authorized_at is not None:
            return False
        self._row(container_id).cancelled_at = at
        self.release(container_id)
        return True

    def record_window(
        self,
        *,
        container_id: str,
        started_at: datetime,
        ended_at: datetime,
        usage_record_ids: Sequence[str],
    ) -> None:
        row = self._row(container_id)
        identity = (container_id, to_utc(started_at))
        records = sorted(set(usage_record_ids))
        existing = self.session.get(BillingFundingWindowTable, identity)
        if existing is not None:
            if (
                to_utc(existing.ended_at) != to_utc(ended_at)
                or existing.usage_record_ids != records
            ):
                raise ConflictError("a metering window cannot be repeated with different records")
        else:
            overlap = self.session.scalar(
                select(BillingFundingWindowTable.container_id)
                .where(
                    BillingFundingWindowTable.container_id == container_id,
                    BillingFundingWindowTable.started_at < ended_at,
                    BillingFundingWindowTable.ended_at > started_at,
                )
                .limit(1)
            )
            if overlap is not None:
                raise ConflictError("complete metering windows cannot overlap")
            self.session.add(
                BillingFundingWindowTable(
                    container_id=container_id,
                    started_at=started_at,
                    ended_at=ended_at,
                    usage_record_ids=list(records),
                )
            )
        self.session.flush()
        if row.metered_through is None:
            row.metered_through = self.session.scalar(
                select(func.min(BillingLedgerSegmentTable.span_started_at)).where(
                    BillingLedgerSegmentTable.subject_type == "container",
                    BillingLedgerSegmentTable.subject_id == container_id,
                    BillingLedgerSegmentTable.dimension == BilledDimension.ComputeRuntime.value,
                )
            )
        windows = self.session.scalars(
            select(BillingFundingWindowTable)
            .where(BillingFundingWindowTable.container_id == container_id)
            .order_by(BillingFundingWindowTable.started_at)
        )
        for window in windows:
            if row.metered_through is not None and to_utc(window.started_at) == to_utc(
                row.metered_through
            ):
                row.metered_through = window.ended_at
        self.session.flush()

    def observe_terminal(self, *, container_id: str, at: datetime) -> None:
        row = self._row(container_id)
        if row.terminal_at is not None and to_utc(row.terminal_at) != to_utc(at):
            raise ConflictError("runtime exit proof cannot change on retry")
        row.terminal_at = at
        self.session.flush()

    def credited_since(self, *, container_id: str, since: datetime) -> int:
        return int(
            self.session.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            BillingCreditAllocationTable.amount_nanos,
                        ),
                        0,
                    )
                )
                .join(
                    BillingLedgerSegmentTable,
                    BillingLedgerSegmentTable.id == BillingCreditAllocationTable.ledger_segment_id,
                )
                .where(
                    BillingLedgerSegmentTable.subject_type == "container",
                    BillingLedgerSegmentTable.subject_id == container_id,
                    BillingLedgerSegmentTable.dimension == BilledDimension.ComputeRuntime.value,
                    BillingLedgerSegmentTable.segment_started_at >= since,
                )
            )
            or 0
        )

    def pending_before(self, *, container_id: str, before: datetime) -> int:
        return int(
            self.session.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            BillingLedgerSegmentTable.cost_nanos,
                        ),
                        0,
                    )
                )
                .join(
                    BillingCreditSettlementTable,
                    BillingCreditSettlementTable.usage_record_id
                    == BillingLedgerSegmentTable.usage_record_id,
                )
                .where(
                    BillingLedgerSegmentTable.subject_type == "container",
                    BillingLedgerSegmentTable.subject_id == container_id,
                    BillingLedgerSegmentTable.dimension == BilledDimension.ComputeRuntime.value,
                    BillingLedgerSegmentTable.segment_ended_at <= before,
                    BillingCreditSettlementTable.settled_at.is_(None),
                )
            )
            or 0
        )

    def _row(self, container_id: str) -> BillingFundingHoldTable:
        row = self.session.get(BillingFundingHoldTable, container_id)
        if row is None:
            raise NotFoundError("the container has no funded reservation")
        return row


def _hold(row: BillingFundingHoldTable) -> FundingHold:
    return FundingHold(
        row.container_id,
        row.workspace_id,
        row.user_id,
        row.worker_id,
        ContainerShape(
            billing_owner=UsageBillingOwner(row.billing_owner),
            gpu_type=row.gpu_type,
            cpu_millicores=row.cpu_millicores,
            memory_mib=row.memory_mib,
            gpu_count=row.gpu_count,
            rate_class=row.rate_class,
        ),
        row.revision,
        to_utc(row.authorized_at) if row.authorized_at else None,
        to_utc(row.valid_until) if row.valid_until else None,
        to_utc(row.metered_through) if row.metered_through else None,
        to_utc(row.terminal_at) if row.terminal_at else None,
        to_utc(row.cancelled_at) if row.cancelled_at else None,
        to_utc(row.loss_resolved_at) if row.loss_resolved_at else None,
    )


__all__ = ["BillingFundingRepository", "FundingHold"]
