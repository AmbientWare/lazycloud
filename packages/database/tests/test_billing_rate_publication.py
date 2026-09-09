from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.repositories.billing_rates import (
    ComputeRateRepository,
    PlatformRateRepository,
    RatePublication,
)
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_rates import PlatformRateTable
from shared.billing_quotes import (
    ContainerShape,
    LedgerBasis,
    LedgerComponent,
    MeteredSpan,
    PricedSpan,
    price_span,
)
from shared.errors import ConflictError
from shared.timestamps import to_utc, utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageBillingOwner,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from sqlalchemy import func, select

_PRICING_VERSION = "test.republish"
_RATE_AT = timedelta(minutes=1)
_WINDOW_AT = timedelta(minutes=2)
_WINDOW = timedelta(seconds=60)


def test_a_boundary_republished_unchanged_writes_nothing_and_one_republished_otherwise_is_refused(
    isolated_services: ApiServices,
) -> None:
    """The deployment publishes its rate card on every sync, and every sync after
    the first finds its own boundary already there.

    Nothing may move at that point, and nothing may fail either. The boundary has
    priced usage by then, so the ledger's frozen edge is past it and the check
    that refuses a retroactive rate would refuse the very publish that opened it.
    Different figures at the same instant stay refused: there is no un-publish,
    and what is published is what a customer was charged.
    """

    now = utc_now()
    effective_at = now + _RATE_AT
    started_at = now + _WINDOW_AT
    ended_at = started_at + _WINDOW
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        opened = PlatformRateRepository(session).publish(
            pricing_version=_PRICING_VERSION,
            effective_at=effective_at,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
    assert opened is RatePublication.Published

    isolated_services.usage.append(
        UsageRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            resource_type="workspace",
            resource_id=workspace_id,
            metric=UsageMetric.NetworkEgressBytes,
            quantity=1_000,
            unit=UsageUnit.Bytes,
            metadata={
                METERING_WINDOW_STARTED_AT_METADATA_KEY: started_at.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: ended_at.isoformat(),
            },
        )
    )

    with isolated_services.context.database.session() as session:
        frozen = session.scalar(select(func.max(BillingLedgerSegmentTable.segment_ended_at)))
        assert frozen is not None
        assert to_utc(frozen) > effective_at
        rates = PlatformRateRepository(session)
        repeated = rates.publish(
            pricing_version=_PRICING_VERSION,
            effective_at=effective_at,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
        assert repeated is RatePublication.AlreadyPublished
        with pytest.raises(ConflictError):
            rates.publish(
                pricing_version=_PRICING_VERSION,
                effective_at=effective_at,
                nanos_per_egress_byte=Decimal(2),
                nanos_per_volume_byte_second=Decimal(0),
            )

    with isolated_services.context.database.session() as session:
        rows = session.scalars(
            select(PlatformRateTable).where(PlatformRateTable.effective_at == effective_at)
        ).all()
    assert [row.nanos_per_egress_byte for row in rows] == [Decimal(1)]


def test_regional_rates_are_isolated_from_automatic_prices(
    isolated_services: ApiServices,
) -> None:
    now = utc_now()
    shape = ContainerShape(UsageBillingOwner.PlatformFleet, "", 1000, 1024, 0)
    regional = replace(shape, rate_class="eu-central-standard")
    with isolated_services.context.database.session() as session:
        rates = ComputeRateRepository(session)
        for placed, rate in ((shape, Decimal(2)), (regional, Decimal(3))):
            rates.publish(
                billing_owner=placed.billing_owner,
                rate_class=placed.rate_class,
                gpu_type="",
                pricing_version="test.regions",
                effective_at=now,
                nanos_per_container_second=Decimal(0),
                nanos_per_cpu_core_second=rate,
                nanos_per_memory_gib_second=Decimal(0),
                nanos_per_gpu_card_second=Decimal(0),
            )
        span = MeteredSpan(
            component=LedgerComponent.Cpu,
            basis=LedgerBasis.Reserved,
            started_at=now,
            ended_at=now + timedelta(seconds=60),
            quantity=Decimal(60),
        )
        for placed, expected in ((shape, 120), (regional, 180)):
            priced = price_span(
                span,
                rates.quotes_for(
                    shape=placed,
                    components=(LedgerComponent.Cpu,),
                    started_at=span.started_at,
                    ended_at=span.ended_at,
                ),
            )
            assert isinstance(priced, PricedSpan)
            assert priced.cost_nanos == expected
