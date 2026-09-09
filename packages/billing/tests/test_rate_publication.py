from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from billing.rate_publication import publish_metered_rate_history
from database.repositories.billing_rates import (
    ComputeRateRepository,
    PlatformRateRepository,
    RatePublication,
)
from database.tables.billing_ledger import BillingLedgerSegmentTable
from shared.billing_quotes import ContainerShape, LedgerComponent
from shared.billing_rate_card import (
    METERED_RATES_EFFECTIVE_AT,
    PUBLISHED_METERED_RATE_HISTORY,
)
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


@pytest.mark.parametrize("existing_installation", [False, True])
def test_reviewed_cutover_prices_both_sides_and_preserves_completed_charges(
    isolated_services: ApiServices, existing_installation: bool
) -> None:
    old = PUBLISHED_METERED_RATE_HISTORY[0]
    assert old.platform_rate is not None
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        if existing_installation:
            PlatformRateRepository(session).publish(
                pricing_version=old.pricing_version,
                effective_at=old.effective_at,
                nanos_per_egress_byte=old.platform_rate.nanos_per_egress_byte,
                nanos_per_volume_byte_second=old.platform_rate.nanos_per_volume_byte_second,
            )
        else:
            publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)

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
                METERED_RATES_EFFECTIVE_AT - timedelta(minutes=2)
            ).isoformat(),
            METERING_WINDOW_ENDED_AT_METADATA_KEY: (
                METERED_RATES_EFFECTIVE_AT - timedelta(minutes=1)
            ).isoformat(),
        },
    )
    isolated_services.usage.append(before)
    with isolated_services.context.database.session() as session:
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)

    after = before.model_copy(
        update={
            "id": str(uuid4()),
            "metadata": {
                METERING_WINDOW_STARTED_AT_METADATA_KEY: METERED_RATES_EFFECTIVE_AT.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: (
                    METERED_RATES_EFFECTIVE_AT + timedelta(minutes=1)
                ).isoformat(),
            },
        }
    )
    isolated_services.usage.append(after)
    with isolated_services.context.database.session() as session:
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
        repeated = publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
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
    isolated_services: ApiServices,
    pinned: bool,
    preemptible: bool,
    cpu_memory_multiplier: str,
    gpu_multiplier: str,
) -> None:
    started_at = datetime(2026, 9, 9, 1, tzinfo=UTC)
    with isolated_services.context.database.session() as session:
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
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
