from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field

from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import (
    CONNECTED_CLOUD_MANAGEMENT_FEE,
    METERED_RATES_EFFECTIVE_AT,
    NO_CARD_INCLUDED_NANOS,
    NO_CARD_MAX_CPU_CONTAINERS,
    NO_CARD_MAX_GPUS,
    PRICING_VERSION,
    PUBLISHED_GPU_RATES,
    PUBLISHED_METERED_RATE_HISTORY,
    PUBLISHED_PLACEMENT_RATES,
    PUBLISHED_PLANS,
    PUBLISHED_PLATFORM_RATE,
    PUBLISHED_SHAPE_RATES,
    SECONDS_PER_30_DAY_MONTH,
    AllGpuTypes,
    EntitlementLimit,
    PlanEntitlements,
)
from shared.gpu import GpuType
from shared.http.base import HttpModel
from shared.payments import BILLING_CURRENCY
from shared.placement import PlacementRateClass
from shared.usage import UsageBillingOwner


class PlanEntitlementsResponse(HttpModel):
    max_concurrent_cpu_containers: int = Field(gt=0)
    max_concurrent_gpus: int = Field(gt=0)
    gpu_types: list[GpuType] | AllGpuTypes
    """The models the plan may ask for, or `all` for every model on the rate card."""

    max_workspaces: EntitlementLimit
    max_members: EntitlementLimit
    connected_cloud: bool
    custom_domains: bool
    self_hosted: bool
    log_retention_days: int = Field(gt=0)
    region_selection: bool


class PublishedPlanResponse(HttpModel):
    id: BillingPlanId
    name: str
    summary: str
    monthly_nanos: int = Field(ge=0)
    included_nanos: int = Field(ge=0)
    entitlements: PlanEntitlementsResponse
    terms: list[str] = Field(default_factory=list)


class NoPaymentMethodTermsResponse(HttpModel):
    included_nanos: int = Field(ge=0)
    max_concurrent_cpu_containers: int = Field(gt=0)
    max_concurrent_gpus: int = Field(gt=0)


class PublishedShapeRateResponse(HttpModel):
    billing_owner: UsageBillingOwner
    nanos_per_container_hour: int = Field(ge=0)
    nanos_per_cpu_core_hour: int = Field(ge=0)
    nanos_per_memory_gib_hour: int = Field(ge=0)


class PublishedGpuRateResponse(HttpModel):
    gpu_type: str
    nanos_per_card_hour: dict[UsageBillingOwner, int]


class PublishedPlatformRateResponse(HttpModel):
    nanos_per_egress_gib: int = Field(ge=0)
    nanos_per_volume_gib_month: int = Field(ge=0)
    storage_month_seconds: int = Field(gt=0)


class PlacementComputeRateResponse(HttpModel):
    billing_owner: UsageBillingOwner
    gpu_type: str
    nanos_per_container_hour: int = Field(ge=0)
    nanos_per_cpu_core_hour: int = Field(ge=0)
    nanos_per_memory_gib_hour: int = Field(ge=0)
    nanos_per_gpu_card_hour: int = Field(ge=0)


class PublishedPlacementRateResponse(HttpModel):
    rate_class: PlacementRateClass
    effective_at: datetime
    pinned: bool
    preemptible: bool
    name: str
    cpu_memory_multiplier: Decimal = Field(gt=0)
    gpu_multiplier: Decimal = Field(gt=0)
    compute_rates: list[PlacementComputeRateResponse]


class PricingCatalogResponse(HttpModel):
    pricing_version: str
    metered_rates_effective_at: datetime
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    connected_cloud_management_fee_percent: int = Field(ge=0, le=100)
    no_payment_method: NoPaymentMethodTermsResponse
    plans: list[PublishedPlanResponse]
    shape_rates: list[PublishedShapeRateResponse]
    gpu_rates: list[PublishedGpuRateResponse]
    platform_rate: PublishedPlatformRateResponse
    placement_rates: list[PublishedPlacementRateResponse]


def _entitlements_response(entitlements: PlanEntitlements) -> PlanEntitlementsResponse:
    return PlanEntitlementsResponse(
        max_concurrent_cpu_containers=entitlements.max_concurrent_cpu_containers,
        max_concurrent_gpus=entitlements.max_concurrent_gpus,
        gpu_types=(
            "all" if entitlements.gpu_types == "all" else list(entitlements.allowed_gpu_types)
        ),
        max_workspaces=entitlements.max_workspaces,
        max_members=entitlements.max_members,
        connected_cloud=entitlements.connected_cloud,
        custom_domains=entitlements.custom_domains,
        self_hosted=entitlements.self_hosted,
        log_retention_days=entitlements.log_retention_days,
        region_selection=entitlements.region_selection,
    )


def pricing_catalog_response() -> PricingCatalogResponse:
    fee_percent = CONNECTED_CLOUD_MANAGEMENT_FEE * 100
    if fee_percent != fee_percent.to_integral_value():
        raise ValueError("the connected-cloud fee is not a whole percentage")
    return PricingCatalogResponse(
        pricing_version=PRICING_VERSION,
        metered_rates_effective_at=METERED_RATES_EFFECTIVE_AT,
        currency=BILLING_CURRENCY,
        placement_rates=[
            PublishedPlacementRateResponse(
                rate_class=placement.rate_class,
                effective_at=max(
                    card.effective_at
                    for card in PUBLISHED_METERED_RATE_HISTORY
                    if any(rate.rate_class == placement.rate_class for rate in card.compute_rates)
                ),
                pinned=placement.pinned,
                preemptible=placement.preemptible,
                name=placement.name,
                cpu_memory_multiplier=placement.cpu_memory_multiplier,
                gpu_multiplier=placement.gpu_multiplier,
                compute_rates=[
                    PlacementComputeRateResponse(
                        billing_owner=rate.billing_owner,
                        gpu_type=rate.gpu_type,
                        nanos_per_container_hour=rate.nanos_per_container_hour,
                        nanos_per_cpu_core_hour=rate.nanos_per_cpu_core_hour,
                        nanos_per_memory_gib_hour=rate.nanos_per_memory_gib_hour,
                        nanos_per_gpu_card_hour=rate.nanos_per_gpu_card_hour,
                    )
                    for rate in placement.compute_rates
                ],
            )
            for placement in PUBLISHED_PLACEMENT_RATES
        ],
        connected_cloud_management_fee_percent=int(fee_percent),
        no_payment_method=NoPaymentMethodTermsResponse(
            included_nanos=NO_CARD_INCLUDED_NANOS,
            max_concurrent_cpu_containers=NO_CARD_MAX_CPU_CONTAINERS,
            max_concurrent_gpus=NO_CARD_MAX_GPUS,
        ),
        plans=[
            PublishedPlanResponse(
                id=plan.id,
                name=plan.name,
                summary=plan.summary,
                monthly_nanos=plan.monthly_nanos,
                included_nanos=plan.included_nanos,
                entitlements=_entitlements_response(plan.entitlements),
                terms=list(plan.terms),
            )
            for plan in PUBLISHED_PLANS
        ],
        shape_rates=[
            PublishedShapeRateResponse(
                billing_owner=rate.billing_owner,
                nanos_per_container_hour=rate.nanos_per_container_hour,
                nanos_per_cpu_core_hour=rate.nanos_per_cpu_core_hour,
                nanos_per_memory_gib_hour=rate.nanos_per_memory_gib_hour,
            )
            for rate in PUBLISHED_SHAPE_RATES
        ],
        gpu_rates=[
            PublishedGpuRateResponse(
                gpu_type=rate.gpu_type,
                nanos_per_card_hour={
                    owner: rate.nanos_per_card_hour(owner) for owner in UsageBillingOwner
                },
            )
            for rate in PUBLISHED_GPU_RATES
        ],
        platform_rate=PublishedPlatformRateResponse(
            nanos_per_egress_gib=PUBLISHED_PLATFORM_RATE.nanos_per_egress_gib,
            nanos_per_volume_gib_month=PUBLISHED_PLATFORM_RATE.nanos_per_volume_gib_month,
            storage_month_seconds=SECONDS_PER_30_DAY_MONTH,
        ),
    )


__all__ = [
    "NoPaymentMethodTermsResponse",
    "PlacementComputeRateResponse",
    "PlanEntitlementsResponse",
    "PricingCatalogResponse",
    "PublishedGpuRateResponse",
    "PublishedPlacementRateResponse",
    "PublishedPlanResponse",
    "PublishedPlatformRateResponse",
    "PublishedShapeRateResponse",
    "pricing_catalog_response",
]
