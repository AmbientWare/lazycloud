from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from billing.rate_publication import publish_metered_rate_history
from database.context import ServiceContext
from database.repositories.billing_rates import (
    ComputeRateRepository,
    PlatformRateRepository,
    RatePublication,
)
from database.tables.billing_ledger import BillingLedgerSegmentTable
from observability.usage import UsageService
from shared.billing_quotes import BYTES_PER_GIB, ContainerShape, LedgerComponent
from shared.billing_rate_card import (
    PUBLISHED_METERED_RATE_HISTORY,
    STORED_RATE_STEP,
)
from shared.http.pricing import pricing_catalog_response
from shared.placement import placement_rate_class
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageBillingOwner,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from sqlalchemy import select

from database import DatabaseClient


@pytest.mark.parametrize("existing_installation", [False, True])
def test_reviewed_cutover_prices_both_sides_and_preserves_completed_charges(
    workspace_database: DatabaseClient, tmp_path: Path, existing_installation: bool
) -> None:
    service_context = ServiceContext.create(workspace_database, root=tmp_path, create_schema=False)
    old = PUBLISHED_METERED_RATE_HISTORY[0]
    assert old.platform_rate is not None
    transfer_boundary = next(
        card.effective_at
        for card in PUBLISHED_METERED_RATE_HISTORY
        if card.platform_rate is not None and card.platform_rate.nanos_per_egress_gib > 0
    )
    with service_context.database.session() as session:
        workspace_id = service_context.default_workspace_id(session)
        if existing_installation:
            PlatformRateRepository(session).publish(
                pricing_version=old.pricing_version,
                effective_at=old.effective_at,
                nanos_per_egress_byte=old.platform_rate.nanos_per_egress_byte,
                nanos_per_volume_byte_second=old.platform_rate.nanos_per_volume_byte_second,
            )
        else:
            publish_metered_rate_history(session)

    before = UsageRecord(
        id=str(uuid4()),
        workspace_id=workspace_id,
        resource_type="workspace",
        resource_id=workspace_id,
        metric=UsageMetric.NetworkEgressBytes,
        quantity=1_073_741_824,
        unit=UsageUnit.Bytes,
        metadata={
            METERING_WINDOW_STARTED_AT_METADATA_KEY: (
                transfer_boundary - timedelta(minutes=2)
            ).isoformat(),
            METERING_WINDOW_ENDED_AT_METADATA_KEY: (
                transfer_boundary - timedelta(minutes=1)
            ).isoformat(),
        },
    )
    UsageService(service_context).append(before)
    with service_context.database.session() as session:
        publish_metered_rate_history(session)

    after = before.model_copy(
        update={
            "id": str(uuid4()),
            "metadata": {
                METERING_WINDOW_STARTED_AT_METADATA_KEY: transfer_boundary.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: (
                    transfer_boundary + timedelta(minutes=1)
                ).isoformat(),
            },
        }
    )
    UsageService(service_context).append(after)
    with service_context.database.session() as session:
        old_segment = session.scalar(
            select(BillingLedgerSegmentTable).where(
                BillingLedgerSegmentTable.usage_record_id == before.id
            )
        )
        new_segment = session.scalar(
            select(BillingLedgerSegmentTable).where(
                BillingLedgerSegmentTable.usage_record_id == after.id
            )
        )
        assert old_segment is not None and old_segment.cost_nanos == 0
        assert old_segment.pricing_version == old.pricing_version
        assert new_segment is not None and new_segment.cost_nanos == 130_000_000
        assert new_segment.rate_class == "auto"
        repeated = publish_metered_rate_history(session)
        assert all(
            card.platform is None or card.platform is RatePublication.AlreadyPublished
            for card in repeated
        )
        assert all(
            rate.state is RatePublication.AlreadyPublished
            for card in repeated
            for rate in card.compute
        )


@pytest.mark.parametrize(
    ("pinned", "preemptible", "cpu_memory_multiplier", "gpu_multiplier"),
    [
        (False, True, "1", "1"),
        (True, True, "1.5", "1.5"),
        (False, False, "3", "1"),
        (True, False, "4.5", "1.5"),
    ],
)
def test_published_execution_choices_price_each_resource_and_preserve_customer_cloud_fees(
    service_context: ServiceContext,
    pinned: bool,
    preemptible: bool,
    cpu_memory_multiplier: str,
    gpu_multiplier: str,
) -> None:
    started_at = datetime(2026, 9, 9, 1, tzinfo=UTC)
    with service_context.database.session() as session:
        rates = ComputeRateRepository(session)
        for owner in (UsageBillingOwner.PlatformFleet, UsageBillingOwner.ConnectedCloud):
            for component, multiplier in (
                (LedgerComponent.Cpu, cpu_memory_multiplier),
                (LedgerComponent.Memory, cpu_memory_multiplier),
                (LedgerComponent.Gpu, gpu_multiplier),
            ):
                base = rates.quotes_for(
                    shape=ContainerShape(owner, "T4", 1_000, 1_024, 1),
                    components=(component,),
                    started_at=started_at,
                    ended_at=started_at + timedelta(seconds=60),
                )
                selected = rates.quotes_for(
                    shape=ContainerShape(
                        owner,
                        "T4",
                        1_000,
                        1_024,
                        1,
                        rate_class=placement_rate_class(pinned=pinned, preemptible=preemptible),
                    ),
                    components=(component,),
                    started_at=started_at,
                    ended_at=started_at + timedelta(seconds=60),
                )
                assert len(base) == len(selected) == 1
                expected_multiplier = (
                    Decimal(multiplier) if owner is UsageBillingOwner.PlatformFleet else Decimal(1)
                )
                assert (
                    selected[0].rate_nanos_per_unit
                    == base[0].rate_nanos_per_unit * expected_multiplier
                )


def test_published_cutovers_match_stored_quotes(
    service_context: ServiceContext,
) -> None:
    with service_context.database.session() as session:
        rates = ComputeRateRepository(session)
        for at in (
            moment
            for card in PUBLISHED_METERED_RATE_HISTORY[1:]
            for moment in (card.effective_at - timedelta(seconds=1), card.effective_at)
        ):
            catalog = pricing_catalog_response(at=at)
            storage_quotes = PlatformRateRepository(session).quotes_for(
                component=LedgerComponent.VolumeStorage,
                started_at=at,
                ended_at=at + timedelta(seconds=1),
            )
            assert len(storage_quotes) == 1
            monthly_units = BYTES_PER_GIB * catalog.platform_rate.storage_month_seconds
            quoted = Decimal(catalog.platform_rate.nanos_per_volume_gib_month)
            charged = storage_quotes[0].rate_nanos_per_unit * monthly_units
            assert Decimal(0) <= quoted - charged < STORED_RATE_STEP * monthly_units
            assert all(placement.effective_at <= at for placement in catalog.placement_rates)
            for placement in catalog.placement_rates:
                selected = next(
                    rate
                    for rate in placement.compute_rates
                    if rate.billing_owner is UsageBillingOwner.PlatformFleet
                    and rate.gpu_type == "H100"
                )
                shape = ContainerShape(
                    UsageBillingOwner.PlatformFleet,
                    "H100",
                    1_000,
                    1_024,
                    1,
                    rate_class=placement.rate_class,
                )
                for component, hourly in (
                    (LedgerComponent.Cpu, selected.nanos_per_cpu_core_hour),
                    (LedgerComponent.Memory, selected.nanos_per_memory_gib_hour),
                    (LedgerComponent.Gpu, selected.nanos_per_gpu_card_hour),
                ):
                    quotes = rates.quotes_for(
                        shape=shape,
                        components=(component,),
                        started_at=at,
                        ended_at=at + timedelta(seconds=1),
                    )
                    assert len(quotes) == 1
                    error = Decimal(hourly) / 3_600 - quotes[0].rate_nanos_per_unit
                    assert Decimal(0) <= error < STORED_RATE_STEP
