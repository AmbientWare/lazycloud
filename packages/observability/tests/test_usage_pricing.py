from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_ledger import (
    BillingLedgerRepository,
    ContainerBillingShapeRepository,
)
from database.repositories.billing_rates import ComputeRateRepository, PlatformRateRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.observability import UsageRecordTable
from observability.usage_pricing import REPRICE_REFUSED_ACTION, UNPRICED_SPAN_ACTION
from shared.billing_quotes import ContainerShape, LedgerBasis, LedgerComponent
from shared.billing_rate_card import FREE_PLAN_INCLUDED_NANOS
from shared.containers import ContainerRecord
from shared.errors import InvalidInputError
from shared.events import EventLevel
from shared.timestamps import to_utc, utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageBillingOwner,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from sqlalchemy import select
from tests.service_fixtures import unbilled_account

# Rates only ever take effect in the future, so the usage they price is later
# still. The offsets are the smallest that keep both facts true for a run.
_RATE_ONE_AT = timedelta(minutes=1)
_RATE_TWO_AT = timedelta(minutes=3)
_WINDOW_AT = timedelta(minutes=2)
_WINDOW = timedelta(seconds=120)

_CPU_MILLICORES = 2_000
_MEMORY_MIB = 4_096


def _shaped_container(services: ApiServices, *, shape: ContainerShape) -> tuple[str, str]:
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="priced-runtime",
                image="",
                command=[],
                workspace_id=workspace_id,
            )
        )
        ContainerBillingShapeRepository(session).record(
            container_id=container.id,
            workspace_id=workspace_id,
            shape=shape,
        )
    return workspace_id, container.id


def _usage(
    *,
    workspace_id: str,
    resource_id: str,
    metric: UsageMetric,
    unit: UsageUnit,
    quantity: float,
    started_at: datetime,
    ended_at: datetime,
    labels: dict[str, str] | None = None,
) -> UsageRecord:
    return UsageRecord(
        id=str(uuid4()),
        workspace_id=workspace_id,
        resource_type="container",
        resource_id=resource_id,
        metric=metric,
        quantity=quantity,
        unit=unit,
        labels={"app_id": "app-priced", "stub_id": "stub-priced", **(labels or {})},
        metadata={
            METERING_WINDOW_STARTED_AT_METADATA_KEY: started_at.isoformat(),
            METERING_WINDOW_ENDED_AT_METADATA_KEY: ended_at.isoformat(),
        },
    )


# What the worker claims it ran, deliberately unlike the shape the control plane
# recorded: these labels are telemetry and must decide no money.
_WORKER_CLAIMED_LABELS = {
    "billing_owner": UsageBillingOwner.SelfHosted.value,
    "cpu_millicores": "1",
    "mem_mb": "1",
    "gpu_count": "0",
}


def _segments(services: ApiServices, usage_record_id: str) -> list[BillingLedgerSegmentTable]:
    with services.context.database.session() as session:
        return list(
            session.scalars(
                select(BillingLedgerSegmentTable)
                .where(BillingLedgerSegmentTable.usage_record_id == usage_record_id)
                .order_by(BillingLedgerSegmentTable.segment_index)
            )
        )


def _quantity_of(
    segments: Sequence[BillingLedgerSegmentTable], component: LedgerComponent
) -> Decimal:
    return sum(
        (segment.quantity for segment in segments if segment.component == component.value),
        Decimal(0),
    )


def _cost_of(segments: Sequence[BillingLedgerSegmentTable], component: LedgerComponent) -> int:
    return sum(segment.cost_nanos for segment in segments if segment.component == component.value)


def test_compute_usage_crossing_a_rate_change_prices_as_tiling_segments(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    shape = ContainerShape(
        billing_owner=UsageBillingOwner.PlatformFleet,
        gpu_type="",
        cpu_millicores=_CPU_MILLICORES,
        memory_mib=_MEMORY_MIB,
        gpu_count=0,
    )
    workspace_id, container_id = _shaped_container(isolated_services, shape=shape)
    with isolated_services.context.database.session() as session:
        rates = ComputeRateRepository(session)
        for effective_at, version, per_core_second in (
            (now + _RATE_ONE_AT, "test.a", Decimal(10)),
            (now + _RATE_TWO_AT, "test.b", Decimal(30)),
        ):
            rates.publish(
                billing_owner=UsageBillingOwner.PlatformFleet,
                gpu_type="",
                pricing_version=version,
                effective_at=effective_at,
                nanos_per_container_second=Decimal(0),
                nanos_per_cpu_core_second=per_core_second,
                nanos_per_memory_gib_second=Decimal(0),
                nanos_per_gpu_card_second=Decimal(0),
            )
    started_at = now + _WINDOW_AT
    ended_at = started_at + _WINDOW
    record = _usage(
        workspace_id=workspace_id,
        resource_id=container_id,
        metric=UsageMetric.ContainerDurationMilliseconds,
        unit=UsageUnit.Milliseconds,
        quantity=_WINDOW.total_seconds() * 1_000,
        started_at=started_at,
        ended_at=ended_at,
        labels=_WORKER_CLAIMED_LABELS,
    )

    saved = isolated_services.usage.append(record)

    segments = _segments(isolated_services, saved.id)
    cpu = [segment for segment in segments if segment.component == LedgerComponent.Cpu.value]
    assert [segment.pricing_version for segment in cpu] == ["test.a", "test.b"]
    # Each half costs the quote covering it times the capacity that half held:
    # two cores for sixty seconds at 10 nanos, then at 30. The worker's own claim
    # of one millicore and self-hosted hardware moves neither figure.
    assert [segment.duration_ms for segment in cpu] == [60_000, 60_000]
    assert [segment.cost_nanos for segment in cpu] == [1_200, 3_600]
    assert to_utc(cpu[0].segment_started_at) == started_at
    assert cpu[0].segment_ended_at == cpu[1].segment_started_at
    assert to_utc(cpu[-1].segment_ended_at) == ended_at
    # Quantities are allocated over each segment's own milliseconds, so a window
    # split at a rate change still charges for the whole window and no more.
    assert _quantity_of(segments, LedgerComponent.Cpu) == Decimal(240)
    assert _quantity_of(segments, LedgerComponent.Memory) == Decimal(480)
    assert _quantity_of(segments, LedgerComponent.ContainerTime) == Decimal(120)
    assert sum(segment.duration_ms for segment in cpu) == int(_WINDOW.total_seconds() * 1_000)
    assert {segment.app_id for segment in segments} == {"app-priced"}
    assert {segment.workload_id for segment in segments} == {"stub-priced"}
    assert {segment.billing_owner for segment in segments} == {
        UsageBillingOwner.PlatformFleet.value
    }


def test_a_window_bills_the_greater_of_the_capacity_held_and_the_capacity_used(
    isolated_services: ApiServices,
) -> None:
    """A reservation is a floor, not a cap, and the two records reach it apart.

    The cgroup quota a container runs under is its burst ceiling rather than its
    request, so a container reserved at one core can burn several. The duration
    record charges the core-seconds the window held; the CPU record charges only
    what the same window used above them. Neither reads the other, and their
    total is what the window actually consumed.
    """

    now = utc_now()
    workspace_id, container_id = _shaped_container(
        isolated_services,
        shape=ContainerShape(
            billing_owner=UsageBillingOwner.PlatformFleet,
            gpu_type="",
            cpu_millicores=1_000,
            memory_mib=1_024,
            gpu_count=0,
        ),
    )
    with isolated_services.context.database.session() as session:
        ComputeRateRepository(session).publish(
            billing_owner=UsageBillingOwner.PlatformFleet,
            gpu_type="",
            pricing_version="test.burst",
            effective_at=now + _RATE_ONE_AT,
            nanos_per_container_second=Decimal(0),
            nanos_per_cpu_core_second=Decimal(100),
            nanos_per_memory_gib_second=Decimal(0),
            nanos_per_gpu_card_second=Decimal(0),
        )

    # One core held for 120s is a floor of 120 core-seconds at 100 nanos apiece.
    reserved_cost = 120 * 100
    for offset, measured, expected in (
        (_WINDOW_AT, Decimal(300), 300 * 100),
        (_WINDOW_AT + 2 * _WINDOW, Decimal(30), reserved_cost),
    ):
        started_at = now + offset
        ended_at = started_at + _WINDOW
        duration = isolated_services.usage.append(
            _usage(
                workspace_id=workspace_id,
                resource_id=container_id,
                metric=UsageMetric.ContainerDurationMilliseconds,
                unit=UsageUnit.Milliseconds,
                quantity=_WINDOW.total_seconds() * 1_000,
                started_at=started_at,
                ended_at=ended_at,
                labels=_WORKER_CLAIMED_LABELS,
            )
        )
        used = isolated_services.usage.append(
            _usage(
                workspace_id=workspace_id,
                resource_id=container_id,
                metric=UsageMetric.CpuUsedCoreSeconds,
                unit=UsageUnit.Seconds,
                quantity=float(measured),
                started_at=started_at,
                ended_at=ended_at,
            )
        )

        held = _segments(isolated_services, duration.id)
        burnt = _segments(isolated_services, used.id)
        assert _cost_of(held, LedgerComponent.Cpu) == reserved_cost
        assert _cost_of(held, LedgerComponent.Cpu) + _cost_of(burnt, LedgerComponent.Cpu) == (
            expected
        )
        assert {segment.basis for segment in held} == {LedgerBasis.Reserved.value}
        assert {segment.basis for segment in burnt} == {LedgerBasis.Measured.value}
        assert _quantity_of(burnt, LedgerComponent.Cpu) == max(Decimal(0), measured - 120)


def test_a_window_with_no_measured_record_bills_the_capacity_it_held(
    isolated_services: ApiServices,
) -> None:
    """Losing the measurement costs the burst above the floor, never the floor.

    Every image build reports no CPU at all, and a container that stayed under
    its reservation emits no measured record either. The floor therefore rides on
    the duration record, whose existence a metering window guarantees, so a
    window that measured nothing still bills every resource it held.
    """

    now = utc_now()
    workspace_id, container_id = _shaped_container(
        isolated_services,
        shape=ContainerShape(
            billing_owner=UsageBillingOwner.PlatformFleet,
            gpu_type="h100",
            cpu_millicores=500,
            memory_mib=2_048,
            gpu_count=2,
        ),
    )
    with isolated_services.context.database.session() as session:
        ComputeRateRepository(session).publish(
            billing_owner=UsageBillingOwner.PlatformFleet,
            gpu_type="h100",
            pricing_version="test.floor",
            effective_at=now + _RATE_ONE_AT,
            nanos_per_container_second=Decimal(7),
            nanos_per_cpu_core_second=Decimal(11),
            nanos_per_memory_gib_second=Decimal(13),
            nanos_per_gpu_card_second=Decimal(17),
        )
    started_at = now + _WINDOW_AT

    saved = isolated_services.usage.append(
        _usage(
            workspace_id=workspace_id,
            resource_id=container_id,
            metric=UsageMetric.ContainerDurationMilliseconds,
            unit=UsageUnit.Milliseconds,
            quantity=_WINDOW.total_seconds() * 1_000,
            started_at=started_at,
            ended_at=started_at + _WINDOW,
            labels=_WORKER_CLAIMED_LABELS,
        )
    )

    segments = _segments(isolated_services, saved.id)
    assert {segment.basis for segment in segments} == {LedgerBasis.Reserved.value}
    assert {
        component: (_quantity_of(segments, component), _cost_of(segments, component))
        for component in (
            LedgerComponent.ContainerTime,
            LedgerComponent.Cpu,
            LedgerComponent.Memory,
            LedgerComponent.Gpu,
        )
    } == {
        LedgerComponent.ContainerTime: (Decimal(120), 840),
        LedgerComponent.Cpu: (Decimal(60), 660),
        LedgerComponent.Memory: (Decimal(240), 3_120),
        LedgerComponent.Gpu: (Decimal(240), 4_080),
    }


def test_egress_is_metered_and_priced_at_an_explicit_zero(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        PlatformRateRepository(session).publish(
            pricing_version="test.a",
            effective_at=now + _RATE_ONE_AT,
            nanos_per_egress_byte=Decimal(0),
            nanos_per_volume_byte_second=Decimal(0),
        )
    started_at = now + _WINDOW_AT
    record = _usage(
        workspace_id=workspace_id,
        resource_id=str(uuid4()),
        metric=UsageMetric.NetworkEgressBytes,
        unit=UsageUnit.Bytes,
        quantity=4_096,
        started_at=started_at,
        ended_at=started_at + _WINDOW,
    )

    saved = isolated_services.usage.append(record)

    segments = _segments(isolated_services, saved.id)
    assert len(segments) == 1
    assert segments[0].rate_nanos_per_unit == Decimal(0)
    assert segments[0].cost_nanos == 0
    assert segments[0].component == LedgerComponent.Egress.value
    assert segments[0].quantity == Decimal(4_096)
    assert segments[0].pricing_version == "test.a"


def test_a_re_recorded_quantity_keeps_the_frozen_cost_and_is_reported(
    isolated_services: ApiServices,
) -> None:
    """The one path that could silently reprice what a customer was shown.

    Usage upserts by record id, so a producer that re-sends an id with a
    different quantity overwrites the usage row. The segments summed from it are
    append-only and stand; the allowance they moved stays where it is; and the
    disagreement is a durable error rather than a figure nobody compared.
    """

    now = utc_now()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        owner_user_id = WorkspaceMemberRepository(session).owner_user_id(workspace_id)
        PlatformRateRepository(session).publish(
            pricing_version="test.a",
            effective_at=now + _RATE_ONE_AT,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
        # The cycle the owner's subscription is in. A cost lands on the period
        # covering it and on no other, so this is also what proves the increment
        # reaches the subscription's own cycle.
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=owner_user_id,
            period_started_at=now - timedelta(days=1),
            period_ended_at=now + timedelta(days=30),
            allowance_nanos=FREE_PLAN_INCLUDED_NANOS,
            funded=True,
        )
    started_at = now + _WINDOW_AT
    record = _usage(
        workspace_id=workspace_id,
        resource_id=str(uuid4()),
        metric=UsageMetric.NetworkEgressBytes,
        unit=UsageUnit.Bytes,
        quantity=4_096,
        started_at=started_at,
        ended_at=started_at + _WINDOW,
    )

    isolated_services.usage.append(record)
    frozen = [
        (segment.id, segment.cost_nanos) for segment in _segments(isolated_services, record.id)
    ]
    isolated_services.usage.append(record.model_copy(update={"quantity": 9_999}))

    assert [
        (segment.id, segment.cost_nanos) for segment in _segments(isolated_services, record.id)
    ] == frozen
    assert sum(cost for _, cost in frozen) == 4_096
    with isolated_services.context.database.session() as session:
        spent = BillingAllowanceRepository(session).current_period(
            user_id=owner_user_id,
            at=started_at,
        )
    assert spent is not None
    assert spent.spent_nanos == 4_096
    reported = isolated_services.events.list(
        workspace_id=None,
        actions=(REPRICE_REFUSED_ACTION,),
    )
    assert len(reported) == 1
    assert reported[0].level is EventLevel.Error
    assert reported[0].data["usage_record_id"] == record.id
    assert reported[0].data["frozen_cost_nanos"] == 4_096
    assert reported[0].data["recomputed_cost_nanos"] == 9_999


def test_usage_no_published_rate_covers_is_recorded_without_cost_and_reported(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    shape = ContainerShape(
        billing_owner=UsageBillingOwner.PlatformFleet,
        gpu_type="",
        cpu_millicores=_CPU_MILLICORES,
        memory_mib=_MEMORY_MIB,
        gpu_count=0,
    )
    workspace_id, container_id = _shaped_container(isolated_services, shape=shape)
    started_at = now + _WINDOW_AT
    record = _usage(
        workspace_id=workspace_id,
        resource_id=container_id,
        metric=UsageMetric.ContainerDurationMilliseconds,
        unit=UsageUnit.Milliseconds,
        quantity=_WINDOW.total_seconds() * 1_000,
        started_at=started_at,
        ended_at=started_at + _WINDOW,
        labels=_WORKER_CLAIMED_LABELS,
    )

    saved = isolated_services.usage.append(record)

    assert _segments(isolated_services, saved.id) == []
    with isolated_services.context.database.session() as session:
        assert session.get(UsageRecordTable, saved.id) is not None
    reported = isolated_services.events.list(
        workspace_id=None,
        actions=(UNPRICED_SPAN_ACTION,),
    )
    assert len(reported) == 1
    assert reported[0].level is EventLevel.Error
    assert reported[0].data["reason"] == "no_published_rate"
    assert reported[0].data["usage_record_id"] == saved.id


def test_a_rate_may_cover_unpriced_instants_but_not_ones_the_ledger_froze(
    isolated_services: ApiServices,
) -> None:
    """A rate reaches back over usage nothing priced, and no further.

    The first rate ever published has to cover usage already metered, or every
    instant before it is permanently unbillable — a rate can never reach it and a
    segment can never be written without one. What it must never do is move a
    figure a customer has already been shown, so the boundary is the ledger's own
    frozen edge rather than the clock.
    """

    now = utc_now()
    shape = ContainerShape(
        billing_owner=UsageBillingOwner.PlatformFleet,
        gpu_type="",
        cpu_millicores=_CPU_MILLICORES,
        memory_mib=_MEMORY_MIB,
        gpu_count=0,
    )
    workspace_id, container_id = _shaped_container(isolated_services, shape=shape)
    started_at = now - timedelta(minutes=10)
    ended_at = started_at + _WINDOW

    with isolated_services.context.database.session() as session:
        ComputeRateRepository(session).publish(
            billing_owner=UsageBillingOwner.PlatformFleet,
            gpu_type="",
            pricing_version="test.backfill",
            effective_at=started_at - timedelta(minutes=1),
            nanos_per_container_second=Decimal(0),
            nanos_per_cpu_core_second=Decimal(5),
            nanos_per_memory_gib_second=Decimal(0),
            nanos_per_gpu_card_second=Decimal(0),
        )

    saved = isolated_services.usage.append(
        _usage(
            workspace_id=workspace_id,
            resource_id=container_id,
            metric=UsageMetric.ContainerDurationMilliseconds,
            unit=UsageUnit.Milliseconds,
            quantity=_WINDOW.total_seconds() * 1_000,
            started_at=started_at,
            ended_at=ended_at,
            labels=_WORKER_CLAIMED_LABELS,
        )
    )

    segments = _segments(isolated_services, saved.id)
    assert {segment.pricing_version for segment in segments} == {"test.backfill"}
    assert sum(segment.cost_nanos for segment in segments) > 0

    # Now the ledger holds a frozen edge, and a rate opening at or before it is
    # refused — that usage has been priced and shown.
    with (
        isolated_services.context.database.session() as session,
        pytest.raises(InvalidInputError),
    ):
        ComputeRateRepository(session).publish(
            billing_owner=UsageBillingOwner.PlatformFleet,
            gpu_type="",
            pricing_version="test.retroactive",
            effective_at=started_at,
            nanos_per_container_second=Decimal(0),
            nanos_per_cpu_core_second=Decimal("9.999"),
            nanos_per_memory_gib_second=Decimal(0),
            nanos_per_gpu_card_second=Decimal(0),
        )


def test_usage_metered_before_any_rate_can_be_priced_once_a_rate_exists(
    isolated_services: ApiServices,
) -> None:
    """The one gap ingest cannot close, and it closes exactly once.

    Usage is priced as it is recorded, so a record that arrived before any rate
    was published is never revisited — no later ingest touches it. Pricing it
    afterwards is an operator's deliberate act over a window they name, and
    running it twice must not write a second set of segments for the same
    milliseconds.
    """

    now = utc_now()
    shape = ContainerShape(
        billing_owner=UsageBillingOwner.PlatformFleet,
        gpu_type="",
        cpu_millicores=_CPU_MILLICORES,
        memory_mib=_MEMORY_MIB,
        gpu_count=0,
    )
    workspace_id, container_id = _shaped_container(isolated_services, shape=shape)
    started_at = now - timedelta(minutes=20)
    ended_at = started_at + _WINDOW

    # Metered with no rate in existence: real seconds, no money.
    saved = isolated_services.usage.append(
        _usage(
            workspace_id=workspace_id,
            resource_id=container_id,
            metric=UsageMetric.ContainerDurationMilliseconds,
            unit=UsageUnit.Milliseconds,
            quantity=_WINDOW.total_seconds() * 1_000,
            started_at=started_at,
            ended_at=ended_at,
            labels=_WORKER_CLAIMED_LABELS,
        )
    )
    assert _segments(isolated_services, saved.id) == []

    with isolated_services.context.database.session() as session:
        ComputeRateRepository(session).publish(
            billing_owner=UsageBillingOwner.PlatformFleet,
            gpu_type="",
            pricing_version="test.first",
            effective_at=started_at - timedelta(minutes=1),
            nanos_per_container_second=Decimal(0),
            nanos_per_cpu_core_second=Decimal(5),
            nanos_per_memory_gib_second=Decimal(0),
            nanos_per_gpu_card_second=Decimal(0),
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        priced, skipped = BillingLedgerRepository(session).price_unpriced_between(
            started_at=started_at - timedelta(hours=1),
            ended_at=now + timedelta(hours=1),
        )
        session.commit()
    assert (priced, skipped) == (1, 0)
    first = _segments(isolated_services, saved.id)
    assert sum(segment.cost_nanos for segment in first) > 0

    # Second run finds it already priced and leaves it exactly as it stands.
    with isolated_services.context.database.session() as session:
        again = BillingLedgerRepository(session).price_unpriced_between(
            started_at=started_at - timedelta(hours=1),
            ended_at=now + timedelta(hours=1),
        )
        session.commit()
    assert again == (0, 0)
    assert [(s.id, s.cost_nanos) for s in _segments(isolated_services, saved.id)] == [
        (s.id, s.cost_nanos) for s in first
    ]


def test_cost_priced_in_the_renewal_gap_lands_on_the_period_that_opens_over_it(
    isolated_services: ApiServices,
) -> None:
    """A cycle ends at the provider before the delivery that opens the next one.

    Pricing runs on its own schedule and does not wait for that delivery, so
    usage in between is priced against terms no row holds yet. Dropping it would
    leave the figure the customer is shown permanently short of the usage their
    next invoice charges them for — the ledger and the meter event both carry it
    regardless.

    So the period picks it up when it opens, from the ledger row written in the
    transaction that priced it.
    """

    now = utc_now()
    # An account provisioning has never reached, so nothing has opened a cycle
    # over it. The default workspace's owner is provisioned by the fixture and
    # holds a period covering now, which is exactly the state this is about the
    # absence of.
    owner_user_id, workspace_id = unbilled_account(isolated_services.context)
    with isolated_services.context.database.session() as session:
        PlatformRateRepository(session).publish(
            pricing_version="test.a",
            effective_at=now + _RATE_ONE_AT,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
    started_at = now + _WINDOW_AT
    isolated_services.usage.append(
        _usage(
            workspace_id=workspace_id,
            resource_id=str(uuid4()),
            metric=UsageMetric.NetworkEgressBytes,
            unit=UsageUnit.Bytes,
            quantity=4_096,
            started_at=started_at,
            ended_at=started_at + _WINDOW,
        )
    )

    with isolated_services.context.database.session() as session:
        # Nothing to count it against while the cycle is between deliveries.
        assert (
            BillingAllowanceRepository(session).current_period(user_id=owner_user_id, at=started_at)
            is None
        )
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=owner_user_id,
            period_started_at=now,
            period_ended_at=now + timedelta(days=30),
            allowance_nanos=FREE_PLAN_INCLUDED_NANOS,
            funded=True,
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        opened = BillingAllowanceRepository(session).current_period(
            user_id=owner_user_id, at=started_at
        )
    assert opened is not None
    assert opened.spent_nanos == 4_096
