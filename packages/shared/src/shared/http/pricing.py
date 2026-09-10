from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_rate_card import (
    CONNECTED_CLOUD_MANAGEMENT_FEE,
    NO_CARD_GPU_TYPES,
    NO_CARD_MAX_CPU_CONTAINERS,
    NO_CARD_MAX_GPUS,
    ONE_TIME_TRIAL_NANOS,
    PUBLISHED_METERED_RATE_HISTORY,
    PUBLISHED_PLANS,
    SECONDS_PER_30_DAY_MONTH,
    TRIAL_VALIDITY_DAYS,
    AllGpuTypes,
    EntitlementLimit,
    PlanEntitlements,
    published_metered_rate_card,
    published_placement_rates,
)
from shared.credit_payments import MAX_CREDIT_PURCHASE_CENTS, MIN_CREDIT_PURCHASE_CENTS
from shared.gpu import NO_GPU, PLATFORM_GPU_TYPES, GpuType
from shared.http.base import HttpModel
from shared.payments import BILLING_CURRENCY
from shared.placement import AUTO_RATE_CLASS, PlacementRateClass
from shared.timestamps import to_utc, utc_now
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
    retention_days: int = Field(gt=0)
    region_selection: bool


class PublishedPlanResponse(HttpModel):
    id: BillingPlanId
    terms_version: SubscriptionTermsVersion
    name: str
    summary: str
    monthly_nanos: int = Field(ge=0)
    included_nanos: int = Field(ge=0)
    entitlements: PlanEntitlementsResponse
    terms: list[str] = Field(default_factory=list)


class NoPaymentMethodTermsResponse(HttpModel):
    max_concurrent_cpu_containers: int = Field(gt=0)
    max_concurrent_gpus: int = Field(gt=0)
    gpu_types: list[GpuType] = Field(min_length=1)


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


class CreditPurchaseTermsResponse(HttpModel):
    minimum_cents: int = Field(gt=0)
    maximum_cents: int = Field(gt=0)


class TrialTermsResponse(HttpModel):
    amount_nanos: int = Field(gt=0)
    duration_days: int = Field(gt=0)
    one_time: Literal[True] = True


class PricingCatalogResponse(HttpModel):
    trial: TrialTermsResponse
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
    credit_purchase: CreditPurchaseTermsResponse


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
        retention_days=entitlements.retention_days,
        region_selection=entitlements.region_selection,
    )


def pricing_catalog_response(*, at: datetime | None = None) -> PricingCatalogResponse:
    moment = to_utc(at) if at is not None else utc_now()
    active = published_metered_rate_card(moment)
    platform_rate = active.platform_rate
    placements = published_placement_rates(active.compute_rates)
    automatic = tuple(rate for rate in active.compute_rates if rate.rate_class == AUTO_RATE_CLASS)
    fee_percent = CONNECTED_CLOUD_MANAGEMENT_FEE * 100
    if fee_percent != fee_percent.to_integral_value():
        raise ValueError("the connected-cloud fee is not a whole percentage")
    return PricingCatalogResponse(
        trial=TrialTermsResponse(
            amount_nanos=ONE_TIME_TRIAL_NANOS,
            duration_days=TRIAL_VALIDITY_DAYS,
        ),
        credit_purchase=CreditPurchaseTermsResponse(
            minimum_cents=MIN_CREDIT_PURCHASE_CENTS,
            maximum_cents=MAX_CREDIT_PURCHASE_CENTS,
        ),
        pricing_version=active.pricing_version,
        metered_rates_effective_at=active.effective_at,
        currency=BILLING_CURRENCY,
        placement_rates=[
            PublishedPlacementRateResponse(
                rate_class=placement.rate_class,
                effective_at=max(
                    card.effective_at
                    for card in PUBLISHED_METERED_RATE_HISTORY
                    if card.effective_at <= moment
                    and any(rate.rate_class == placement.rate_class for rate in card.compute_rates)
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
                    if rate.billing_owner is not UsageBillingOwner.PlatformFleet
                    or rate.gpu_type == NO_GPU
                    or rate.gpu_type in PLATFORM_GPU_TYPES
                ],
            )
            for placement in placements
        ],
        connected_cloud_management_fee_percent=int(fee_percent),
        no_payment_method=NoPaymentMethodTermsResponse(
            gpu_types=sorted(NO_CARD_GPU_TYPES),
            max_concurrent_cpu_containers=NO_CARD_MAX_CPU_CONTAINERS,
            max_concurrent_gpus=NO_CARD_MAX_GPUS,
        ),
        plans=[
            PublishedPlanResponse(
                id=plan.id,
                terms_version=plan.terms_version,
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
            for rate in automatic
            if rate.gpu_type == NO_GPU
        ],
        gpu_rates=[
            PublishedGpuRateResponse(
                gpu_type=gpu_type,
                nanos_per_card_hour={
                    rate.billing_owner: rate.nanos_per_gpu_card_hour
                    for rate in automatic
                    if rate.gpu_type == gpu_type
                },
            )
            for gpu_type in PLATFORM_GPU_TYPES
        ],
        platform_rate=PublishedPlatformRateResponse(
            nanos_per_egress_gib=platform_rate.nanos_per_egress_gib,
            nanos_per_volume_gib_month=platform_rate.nanos_per_volume_gib_month,
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
