from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.repositories.billing_rates import (
    ComputeRateRepository,
    PlatformRateRepository,
    RatePublication,
)
from shared.billing_rate_card import (
    PUBLISHED_METERED_RATE_HISTORY,
    PublishedComputeRate,
)
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ComputeRatePublication:
    rate: PublishedComputeRate
    state: RatePublication


@dataclass(frozen=True, slots=True)
class MeteredRatePublication:
    effective_at: datetime
    pricing_version: str
    compute: tuple[ComputeRatePublication, ...]
    platform: RatePublication | None


def publish_metered_rate_history(session: Session) -> tuple[MeteredRatePublication, ...]:
    compute = ComputeRateRepository(session)
    platform = PlatformRateRepository(session)
    publications: list[MeteredRatePublication] = []
    for card in PUBLISHED_METERED_RATE_HISTORY:
        rates = tuple(
            ComputeRatePublication(
                rate=rate,
                state=compute.publish(
                    billing_owner=rate.billing_owner,
                    rate_class=rate.rate_class,
                    gpu_type=rate.gpu_type,
                    pricing_version=card.pricing_version,
                    effective_at=card.effective_at,
                    nanos_per_container_second=rate.nanos_per_container_second,
                    nanos_per_cpu_core_second=rate.nanos_per_cpu_core_second,
                    nanos_per_memory_gib_second=rate.nanos_per_memory_gib_second,
                    nanos_per_gpu_card_second=rate.nanos_per_gpu_card_second,
                ),
            )
            for rate in card.compute_rates
        )
        state = (
            platform.publish(
                pricing_version=card.pricing_version,
                effective_at=card.effective_at,
                nanos_per_egress_byte=card.platform_rate.nanos_per_egress_byte,
                nanos_per_volume_byte_second=card.platform_rate.nanos_per_volume_byte_second,
            )
            if card.platform_rate is not None
            else None
        )
        publications.append(
            MeteredRatePublication(card.effective_at, card.pricing_version, rates, state)
        )
    return tuple(publications)
