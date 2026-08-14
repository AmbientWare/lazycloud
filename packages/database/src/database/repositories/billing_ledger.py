from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_rates import ComputeRateRepository, PlatformRateRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.tables.base import DatabaseBase
from database.tables.billing import BillingAccountTable
from database.tables.billing_ledger import (
    BillingLedgerSegmentTable,
    ContainerBillingShapeTable,
)
from database.tables.billing_outbox import BillingMeterOutboxTable
from database.tables.observability import UsageRecordTable
from pydantic import JsonValue
from shared.billing_quotes import (
    BILLED_METRICS,
    BilledDimension,
    BilledUsage,
    ContainerShape,
    LedgerBasis,
    MeteredSpan,
    PricedSegment,
    PricedSpan,
    SpanPricing,
    UnpricedReason,
    UnpricedSpan,
    elapsed_seconds,
    measured_excess,
    measured_quantity,
    price_span,
    reserved_quantity,
)
from shared.payments import METER_EVENT_NAMES
from shared.timestamps import to_utc, utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageBillingOwner,
    UsageRecord,
)
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import Insert as SqliteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

_CONTAINER_SUBJECT = "container"

type SegmentValue = str | int | float | Decimal | datetime | None


def _insert(session: Session, table: type[DatabaseBase]) -> PostgresInsert | SqliteInsert:
    """An insert statement that can be told to ignore a conflict.

    Only the dialect's own constructor carries `on_conflict_do_nothing`, and
    every write below relies on it: a placement is decided once, a segment is
    frozen once, and a record owes the provider one meter event.
    """

    if session.get_bind().dialect.name == "postgresql":
        return postgresql_insert(table)
    return sqlite_insert(table)


@dataclass(frozen=True, slots=True)
class ContainerBillingShapeRepository:
    """Where the control plane records what it placed.

    Written in the transaction that records the runtime worker, so a container
    that is running has a shape and one that never started has none. Nothing a
    worker reports reaches this table.
    """

    session: Session

    def record(self, *, container_id: str, workspace_id: str, shape: ContainerShape) -> None:
        values: dict[str, SegmentValue] = {
            "container_id": container_id,
            "workspace_id": workspace_id,
            "billing_owner": shape.billing_owner.value,
            "gpu_type": shape.gpu_type,
            "cpu_millicores": shape.cpu_millicores,
            "memory_mib": shape.memory_mib,
            "gpu_count": shape.gpu_count,
        }
        # A placement is decided once. A retried assignment restates the same
        # shape, and accepting a differing one would reprice a live container.
        self.session.execute(
            _insert(self.session, ContainerBillingShapeTable)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ContainerBillingShapeTable.container_id])
        )
        self.session.flush()

    def shape_for(self, container_id: str) -> ContainerShape | None:
        row = self.session.get(ContainerBillingShapeTable, container_id)
        if row is None:
            return None
        return ContainerShape(
            billing_owner=UsageBillingOwner(row.billing_owner),
            gpu_type=row.gpu_type,
            cpu_millicores=row.cpu_millicores,
            memory_mib=row.memory_mib,
            gpu_count=row.gpu_count,
        )


@dataclass(frozen=True, slots=True)
class _RecordedSegments:
    """The rows one call wrote, never the ones it merely computed."""

    count: int
    cost_nanos: int
    pricing_versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FrozenSpan:
    """What the ledger already holds for a record it was asked to price again.

    Segments are append-only, so a record priced once is priced: what a customer
    was shown and what the provider was metered is `cost_nanos`, whatever a later
    computation over the same record makes of it. `recomputed_cost_nanos` is that
    later figure and charges nothing — it is carried only so a disagreement can be
    named, which is what a quantity re-sent under an id that was already priced
    produces.
    """

    dimension: BilledDimension
    cost_nanos: int
    recomputed_cost_nanos: int

    @property
    def disagrees(self) -> bool:
        return self.cost_nanos != self.recomputed_cost_nanos


@dataclass(frozen=True, slots=True)
class BillingLedgerRepository:
    """Turns one metered record into the cost it freezes.

    Runs in the caller's session — the transaction that wrote the usage record it
    prices — because a crash between the two would either lose money or count it
    twice.
    """

    session: Session

    def price_unpriced_between(
        self, *, started_at: datetime, ended_at: datetime
    ) -> tuple[int, int]:
        """Price billable usage in this window that has no segment yet.

        First pricing, never repricing: a record with any segment is skipped, so
        the append-only rule holds by construction rather than by care. Nothing
        in production needs this — usage is priced as it is recorded — so it
        exists for the one case that cannot be: usage metered before any rate
        was published, which no later ingest will revisit. An operator runs it
        once, over a window they name.

        The window is when usage was *recorded*, not when it ran: that is the
        question an operator can answer from an outage, and it is the column a
        record arrives with.

        Returns how many records segments were written for and how many were
        found and left alone — still unpriced, because no rate or no placement
        covered them. Counting the second as priced would report an outage as
        closed by the run that failed to close it.
        """

        if ended_at <= started_at:
            raise ValueError("a repricing window must end after it starts")
        rows = self.session.scalars(
            select(UsageRecordTable)
            .where(
                UsageRecordTable.created_at >= started_at,
                UsageRecordTable.created_at < ended_at,
                UsageRecordTable.metric.in_([metric.value for metric in BILLED_METRICS]),
                ~select(BillingLedgerSegmentTable.id)
                .where(BillingLedgerSegmentTable.usage_record_id == UsageRecordTable.id)
                .exists(),
            )
            .order_by(UsageRecordTable.created_at.asc())
        ).all()

        priced = 0
        for row in rows:
            if isinstance(self.price_record(UsageRecord.model_validate(row.payload)), PricedSpan):
                priced += 1
        return priced, len(rows) - priced

    def price_record(self, record: UsageRecord) -> SpanPricing | FrozenSpan | None:
        """Price one usage record, or answer that it is not money.

        One record alone, always. Nothing here reads another record, joins on
        time or looks a sibling window up: the cost is a function of this
        record's quantity, this record's metering window, the placement recorded
        for its container, and the rates in force over that window. A duration
        record charges the capacity its window held; a CPU or memory record
        charges only what the same window used above the same floor. The two
        therefore sum to `max(reserved, measured)` whatever order they arrive in,
        whether they share a transaction, and whether the measured one arrives at
        all.

        `None` where the metric produces no charge at all: those records are
        attribution and telemetry, and there is no dimension to look a rate up
        for. `UnpricedSpan` is the different answer — something billable that no
        published rate covered. It writes nothing, and the caller records it as a
        durable error and publishes the missing rate.

        `FrozenSpan` where the ledger already held segments for this record. The
        answer is then what is on disk rather than what was just computed, and
        neither the allowance nor the meter outbox moves again. The two figures
        agreeing is the ordinary idempotent path; them disagreeing means a
        quantity was re-sent under an id that was already priced, and the caller
        records that.
        """

        billed = BILLED_METRICS.get(record.metric)
        if billed is None:
            return None
        window = _metering_window(record)
        if window is None:
            moment = to_utc(record.created_at)
            return UnpricedSpan(
                dimension=billed.dimension,
                gap_started_at=moment,
                gap_ended_at=moment,
                reason=UnpricedReason.NoMeteringWindow,
            )
        started_at, ended_at = window
        shape = self._shape(record)
        quantity = Decimal(str(record.quantity))
        if billed.dimension is BilledDimension.ComputeRuntime:
            if shape is None:
                return UnpricedSpan(
                    dimension=billed.dimension,
                    gap_started_at=started_at,
                    gap_ended_at=ended_at,
                    reason=UnpricedReason.NoRecordedPlacement,
                )
            quotes = ComputeRateRepository(self.session).quotes_for(
                shape=shape,
                components=billed.components,
                started_at=started_at,
                ended_at=ended_at,
            )
            spans = _compute_spans(
                billed=billed,
                shape=shape,
                started_at=started_at,
                ended_at=ended_at,
                quantity=quantity,
            )
        else:
            component = billed.components[0]
            quotes = PlatformRateRepository(self.session).quotes_for(
                component=component,
                started_at=started_at,
                ended_at=ended_at,
            )
            spans = (
                MeteredSpan(
                    component=component,
                    basis=billed.basis,
                    started_at=started_at,
                    ended_at=ended_at,
                    quantity=measured_quantity(component, quantity),
                ),
            )
        priced: list[tuple[MeteredSpan, PricedSpan]] = []
        for span in spans:
            pricing = price_span(span, quotes)
            if isinstance(pricing, UnpricedSpan):
                return pricing
            priced.append((span, pricing))
        owner = WorkspaceMemberRepository(self.session).owner(record.workspace_id)
        if owner is None:
            return UnpricedSpan(
                dimension=billed.dimension,
                gap_started_at=started_at,
                gap_ended_at=ended_at,
                reason=UnpricedReason.NoAccountOwner,
            )
        recorded = self._insert_segments(
            record=record,
            priced=priced,
            shape=shape,
            owner_user_id=owner.user_id,
        )
        whole = PricedSpan(
            segments=tuple(segment for _, pricing in priced for segment in pricing.segments)
        )
        if recorded.count == 0:
            return FrozenSpan(
                dimension=billed.dimension,
                cost_nanos=self._frozen_cost_nanos(record.id),
                recomputed_cost_nanos=whole.cost_nanos,
            )
        BillingAllowanceRepository(self.session).increment(
            user_id=owner.user_id,
            at=started_at,
            cost_nanos=recorded.cost_nanos,
        )
        self._queue_meter_event(
            record=record,
            dimension=billed.dimension,
            occurred_at=started_at,
            recorded=recorded,
            owner_user_id=owner.user_id,
        )
        return whole

    def _shape(self, record: UsageRecord) -> ContainerShape | None:
        if record.resource_type != _CONTAINER_SUBJECT or not _is_uuid(record.resource_id):
            return None
        return ContainerBillingShapeRepository(self.session).shape_for(record.resource_id)

    def _frozen_cost_nanos(self, usage_record_id: str) -> int:
        total = self.session.scalar(
            select(func.coalesce(func.sum(BillingLedgerSegmentTable.cost_nanos), 0)).where(
                BillingLedgerSegmentTable.usage_record_id == usage_record_id
            )
        )
        return int(total or 0)

    def _insert_segments(
        self,
        *,
        record: UsageRecord,
        priced: Sequence[tuple[MeteredSpan, PricedSpan]],
        shape: ContainerShape | None,
        owner_user_id: str,
    ) -> _RecordedSegments:
        rows = [
            _segment_values(
                record=record,
                span=span,
                segment=segment,
                shape=shape,
                owner_user_id=owner_user_id,
            )
            for span, pricing in priced
            for segment in pricing.segments
        ]
        inserted = self.session.execute(
            _insert(self.session, BillingLedgerSegmentTable)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=[
                    BillingLedgerSegmentTable.usage_record_id,
                    BillingLedgerSegmentTable.component,
                    BillingLedgerSegmentTable.segment_index,
                ]
            )
            .returning(
                BillingLedgerSegmentTable.segment_index,
                BillingLedgerSegmentTable.cost_nanos,
                BillingLedgerSegmentTable.pricing_version,
            )
        ).all()
        self.session.flush()
        # By segment index, which is chronological, so the versions read out in
        # the order they took effect rather than in whatever order the insert
        # returned them. Components share an index where they share a rate row,
        # which is every time: one row publishes all four figures.
        ordered = sorted(inserted, key=lambda row: int(row.segment_index))
        return _RecordedSegments(
            count=len(ordered),
            cost_nanos=sum(int(row.cost_nanos) for row in ordered),
            pricing_versions=tuple(dict.fromkeys(str(row.pricing_version) for row in ordered)),
        )

    def _queue_meter_event(
        self,
        *,
        record: UsageRecord,
        dimension: BilledDimension,
        occurred_at: datetime,
        recorded: _RecordedSegments,
        owner_user_id: str,
    ) -> None:
        """Owe the provider one event per priced record, or owe it nothing.

        One event however many components the record wrote: a dimension is one
        meter at the provider, and every component of one record belongs to one
        dimension.

        Owed is what was written, never what was computed beside it, so the
        figure the provider adds up and the figure the ledger holds are one
        number sent twice.

        Nothing is owed for zero. The provider sums these values into a meter, so
        a zero moves no total there, and the $0.00 line a customer reads comes
        from the metered price their subscription carries rather than from events
        against it. A zero row would buy a claim, a request and a retry schedule
        for a charge nobody makes. That a dimension was metered and free is held
        where it decides something: a ledger segment at an explicit rate of zero,
        which a dimension no rate covered never gets.

        An account named nowhere at the provider has nothing to meter there, so
        its ledger is complete without a row here.
        """

        if recorded.cost_nanos == 0:
            return
        provider_customer_id = self.session.scalars(
            select(BillingAccountTable.provider_customer_id).where(
                BillingAccountTable.user_id == owner_user_id
            )
        ).first()
        if not provider_customer_id:
            return
        now = utc_now()
        self.session.execute(
            _insert(self.session, BillingMeterOutboxTable)
            .values(
                id=str(uuid4()),
                workspace_id=record.workspace_id,
                identifier=record.id,
                provider_customer_id=provider_customer_id,
                meter_event_name=METER_EVENT_NAMES[dimension],
                value_nanos=recorded.cost_nanos,
                # Every published version the span drew on. A span that crossed a
                # rate change was priced under both, and naming only one of them
                # is the provider's copy disagreeing with the segments behind it.
                pricing_version=",".join(recorded.pricing_versions),
                occurred_at=occurred_at,
                status="pending",
                attempts=0,
                next_attempt_at=now,
            )
            .on_conflict_do_nothing(index_elements=[BillingMeterOutboxTable.identifier])
        )
        self.session.flush()


def _compute_spans(
    *,
    billed: BilledUsage,
    shape: ContainerShape,
    started_at: datetime,
    ended_at: datetime,
    quantity: Decimal,
) -> tuple[MeteredSpan, ...]:
    """One span per component, over this record's window and no other.

    The floor is recomputed here from the placement and the window rather than
    read off whatever a sibling record charged, which is what makes a stale
    window harmless: a window can only ever be priced against the capacity that
    window held, so evidence from a longer window cannot be charged against a
    shorter one's ground.
    """

    seconds = elapsed_seconds(started_at, ended_at)
    spans: list[MeteredSpan] = []
    for component in billed.components:
        floor = reserved_quantity(component, shape, seconds)
        spans.append(
            MeteredSpan(
                component=component,
                basis=billed.basis,
                started_at=started_at,
                ended_at=ended_at,
                quantity=(
                    floor
                    if billed.basis is LedgerBasis.Reserved
                    else measured_excess(measured_quantity(component, quantity), floor)
                ),
            )
        )
    return tuple(spans)


def _segment_values(
    *,
    record: UsageRecord,
    span: MeteredSpan,
    segment: PricedSegment,
    shape: ContainerShape | None,
    owner_user_id: str,
) -> dict[str, SegmentValue]:
    return {
        "id": str(uuid4()),
        "usage_record_id": record.id,
        "segment_index": segment.index,
        "workspace_id": record.workspace_id,
        "owner_user_id": owner_user_id,
        "dimension": span.dimension.value,
        "component": span.component.value,
        "basis": span.basis.value,
        "subject_type": record.resource_type,
        "subject_id": record.resource_id,
        "app_id": record.labels.get("app_id", ""),
        "workload_id": record.labels.get("stub_id", ""),
        "task_id": record.labels.get("task_id", ""),
        "worker_id": _text(record.metadata.get("worker_id")) or record.labels.get("worker_id", ""),
        "billing_owner": shape.billing_owner.value if shape is not None else "",
        "gpu_type": shape.gpu_type if shape is not None else "",
        "span_started_at": span.started_at,
        "span_ended_at": span.ended_at,
        "segment_started_at": segment.started_at,
        "segment_ended_at": segment.ended_at,
        "duration_ms": segment.duration_ms,
        "quantity": segment.quantity,
        "pricing_version": segment.quote.pricing_version,
        "rate_nanos_per_unit": segment.quote.rate_nanos_per_unit,
        "quote_effective_at": segment.quote.effective_at,
        "quote_valid_until": segment.quote.valid_until,
        "cost_nanos": segment.cost_nanos,
    }


def _metering_window(record: UsageRecord) -> tuple[datetime, datetime] | None:
    started_at = _instant(record.metadata.get(METERING_WINDOW_STARTED_AT_METADATA_KEY))
    ended_at = _instant(record.metadata.get(METERING_WINDOW_ENDED_AT_METADATA_KEY))
    if started_at is None or ended_at is None or ended_at <= started_at:
        return None
    return started_at, ended_at


def _instant(value: JsonValue) -> datetime | None:
    """An interval bound a producer stated, or nothing.

    A naive timestamp is refused rather than assumed to be UTC: it names no
    instant, and guessing one is how a charge lands in the wrong period.
    """

    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return to_utc(moment)


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _is_uuid(value: str) -> bool:
    """Whether a subject id can address the shape table's key at all.

    A resource id is free text on a usage record, and looking one up that is not
    a UUID is the query the column type refuses rather than a miss.
    """

    try:
        UUID(value)
    except ValueError:
        return False
    return True


__all__ = [
    "BillingLedgerRepository",
    "ContainerBillingShapeRepository",
    "FrozenSpan",
]
