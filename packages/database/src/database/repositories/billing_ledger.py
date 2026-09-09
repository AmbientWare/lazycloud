from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_rates import ComputeRateRepository, PlatformRateRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.tables.base import DatabaseBase
from database.tables.billing import BillingAccountTable
from database.tables.billing_funding import BillingFundingHoldTable
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
from shared.errors import ConflictError
from shared.payments import METER_EVENT_NAMES
from shared.placement import AUTO_RATE_CLASS
from shared.timestamps import to_utc, utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageBillingOwner,
    UsageRecord,
    usage_record_id,
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
            "rate_class": shape.rate_class,
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

        row = self.session.get(ContainerBillingShapeTable, container_id)
        if row is None:
            raise RuntimeError("billing shape insert did not persist")
        if row.workspace_id != workspace_id or self.shape_for(container_id) != shape:
            raise ConflictError("a container's recorded billing placement cannot be changed")

    def shape_for(self, container_id: str) -> ContainerShape | None:
        row = self.session.get(ContainerBillingShapeTable, container_id)
        if row is None:
            return None
        return ContainerShape(
            billing_owner=UsageBillingOwner(row.billing_owner),
            rate_class=row.rate_class,
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
        BillingAccountRepository(self.session).get_by_user(owner.user_id, for_update=True)
        cutover = BillingCreditRepository(self.session).cutover(user_id=owner.user_id)
        if cutover is not None:
            priced = [
                (span, _split_credit_boundary(pricing, cutover.effective_at))
                for span, pricing in priced
            ]
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
            self._queue_local_credit(usage_record_id=record.id, owner_user_id=owner.user_id)
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
        self._queue_local_credit(usage_record_id=record.id, owner_user_id=owner.user_id)
        legacy_segments = tuple(
            segment
            for _, pricing in priced
            for segment in pricing.segments
            if cutover is None or segment.started_at < cutover.effective_at
        )
        if legacy_segments:
            self._queue_meter_event(
                workspace_id=record.workspace_id,
                usage_record_id=record.id,
                identifier=record.id,
                dimension=billed.dimension,
                occurred_at=started_at,
                ended_at=max(segment.ended_at for segment in legacy_segments),
                recorded=_RecordedSegments(
                    count=len(legacy_segments),
                    cost_nanos=sum(segment.cost_nanos for segment in legacy_segments),
                    pricing_versions=tuple(
                        dict.fromkeys(segment.quote.pricing_version for segment in legacy_segments)
                    ),
                ),
                owner_user_id=owner.user_id,
            )
        return whole

    def settle_pending_credits(self, *, owner_user_id: str) -> None:
        for record_id in BillingCreditRepository(self.session).pending_records(
            user_id=owner_user_id
        ):
            self._queue_local_credit(usage_record_id=record_id, owner_user_id=owner_user_id)

    def _queue_local_credit(self, *, usage_record_id: str, owner_user_id: str) -> bool:
        credits = BillingCreditRepository(self.session)
        cutover = credits.cutover(user_id=owner_user_id)
        if cutover is None:
            return False
        segments = self.session.scalars(
            select(BillingLedgerSegmentTable)
            .where(
                BillingLedgerSegmentTable.usage_record_id == usage_record_id,
                BillingLedgerSegmentTable.segment_started_at >= cutover.effective_at,
            )
            .order_by(BillingLedgerSegmentTable.segment_started_at, BillingLedgerSegmentTable.id)
        ).all()
        if not segments:
            return False
        account = BillingAccountRepository(self.session).get_by_user(owner_user_id, for_update=True)
        if account is None:
            raise ConflictError("priced usage has no billing account for credit settlement")
        occurred_at = to_utc(segments[0].segment_started_at)
        settled = credits.settle(
            user_id=owner_user_id,
            usage_record_id=usage_record_id,
            funding_confirmed=BillingAllowanceRepository(self.session).credit_confirmed(
                user_id=owner_user_id,
                started_at=occurred_at,
                ended_at=max(to_utc(segment.segment_ended_at) for segment in segments),
            ),
            waived=account.complimentary_since is not None
            or self._lost_execution_usage(usage_record_id, BilledDimension(segments[0].dimension)),
        )
        if settled is not None:
            self._queue_meter_event(
                workspace_id=segments[0].workspace_id,
                usage_record_id=usage_record_id,
                identifier=_credit_meter_identifier(usage_record_id),
                dimension=BilledDimension(segments[0].dimension),
                occurred_at=occurred_at,
                ended_at=max(to_utc(segment.segment_ended_at) for segment in segments),
                recorded=_RecordedSegments(
                    count=len(segments),
                    cost_nanos=settled.payable_nanos,
                    pricing_versions=tuple(
                        dict.fromkeys(segment.pricing_version for segment in segments)
                    ),
                ),
                owner_user_id=owner_user_id,
            )
        return True

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

    def _lost_execution_usage(self, record_id: str, dimension: BilledDimension) -> bool:
        if dimension != BilledDimension.ComputeRuntime:
            return False
        record = self.session.get(UsageRecordTable, record_id)
        if record is None or record.resource_type != _CONTAINER_SUBJECT:
            return False
        hold = self.session.get(BillingFundingHoldTable, record.resource_id)
        return hold is not None and hold.loss_resolved_at is not None

    def _queue_meter_event(
        self,
        *,
        workspace_id: str,
        usage_record_id: str,
        identifier: str,
        dimension: BilledDimension,
        occurred_at: datetime,
        ended_at: datetime,
        recorded: _RecordedSegments,
        owner_user_id: str,
    ) -> None:
        """Queue the frozen payable amount for one settlement interval.

        Its identifier deduplicates retries at the provider. A cutover-crossing
        record has separate legacy and local intervals with the same ledger source.
        Waivers retain gross value as evidence; zero payable needs no event.
        """

        if recorded.cost_nanos == 0:
            return
        account = self.session.execute(
            select(
                BillingAccountTable.provider_customer_id,
                BillingAccountTable.complimentary_since,
            ).where(BillingAccountTable.user_id == owner_user_id)
        ).first()
        if account is None or not account[0]:
            return
        provider_customer_id = str(account[0])
        waived = account[1] is not None or self._lost_execution_usage(usage_record_id, dimension)
        now = utc_now()
        self.session.execute(
            _insert(self.session, BillingMeterOutboxTable)
            .values(
                id=str(uuid4()),
                workspace_id=workspace_id,
                identifier=identifier,
                usage_record_id=usage_record_id,
                provider_customer_id=provider_customer_id,
                meter_event_name=METER_EVENT_NAMES[dimension],
                value_nanos=recorded.cost_nanos,
                # Every published version the span drew on. A span that crossed a
                # rate change was priced under both, and naming only one of them
                # is the provider's copy disagreeing with the segments behind it.
                pricing_version=",".join(recorded.pricing_versions),
                occurred_at=occurred_at,
                metering_ended_at=ended_at,
                status="waived" if waived else "pending",
                attempts=0,
                next_attempt_at=now,
            )
            .on_conflict_do_nothing(index_elements=[BillingMeterOutboxTable.identifier])
        )
        self.session.flush()


def _credit_meter_identifier(record_id: str) -> str:
    return usage_record_id("credit-settlement", record_id)


def _split_credit_boundary(pricing: PricedSpan, boundary: datetime) -> PricedSpan:
    segments: list[PricedSegment] = []
    for segment in pricing.segments:
        if not segment.started_at < boundary < segment.ended_at:
            segments.append(replace(segment, index=len(segments)))
            continue
        duration = (segment.ended_at - segment.started_at) // timedelta(microseconds=1)
        before = (boundary - segment.started_at) // timedelta(microseconds=1)
        quantity = segment.quantity * Decimal(before) / Decimal(duration)
        cost = segment.cost_nanos * before // duration
        duration_ms = segment.duration_ms * before // duration
        segments.extend(
            (
                replace(
                    segment,
                    index=len(segments),
                    ended_at=boundary,
                    duration_ms=duration_ms,
                    quantity=quantity,
                    cost_nanos=cost,
                ),
                replace(
                    segment,
                    index=len(segments) + 1,
                    started_at=boundary,
                    duration_ms=segment.duration_ms - duration_ms,
                    quantity=segment.quantity - quantity,
                    cost_nanos=segment.cost_nanos - cost,
                ),
            )
        )
    return PricedSpan(tuple(segments))


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
        "rate_class": (
            shape.rate_class
            if shape is not None and span.dimension is BilledDimension.ComputeRuntime
            else AUTO_RATE_CLASS
        ),
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
