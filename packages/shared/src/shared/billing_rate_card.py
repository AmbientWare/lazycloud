from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Literal, TypeAlias

from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_quotes import BYTES_PER_GIB, NANOS_PER_USD
from shared.gpu import NO_GPU, SUPPORTED_GPU_TYPES, GpuType
from shared.placement import AUTO_RATE_CLASS, PlacementRateClass, placement_rate_class
from shared.timestamps import to_utc
from shared.usage import UsageBillingOwner

FREE_PLAN_MONTHLY_NANOS = 0

FREE_PLAN_INCLUDED_NANOS = 0
ONE_TIME_TRIAL_NANOS = 5 * NANOS_PER_USD
TRIAL_VALIDITY_DAYS = 30

FREE_PLAN_MAX_CPU_CONTAINERS = 30
"""Published account-wide CPU concurrency, shared across owned workspaces."""

FREE_PLAN_MAX_GPUS = 5
"""Count cards across the account separately from CPU containers."""

FREE_PLAN_GPU_TYPES: frozenset[GpuType] = frozenset({GpuType.T4, GpuType.L4, GpuType.A10G})

FREE_PLAN_MAX_WORKSPACES = 1
FREE_PLAN_MAX_MEMBERS = 1
"""Membership counts include the owner."""

TEAM_PLAN_MAX_CPU_CONTAINERS = 1_000
TEAM_PLAN_MAX_GPUS = 50

UnlimitedEntitlement: TypeAlias = Literal["unlimited"]
EntitlementLimit: TypeAlias = int | UnlimitedEntitlement

AllGpuTypes: TypeAlias = Literal["all"]
GpuTypeEntitlement: TypeAlias = frozenset[GpuType] | AllGpuTypes
"""Which cards a plan may ask for: a named set, or every model the platform rents."""

NO_CARD_MAX_CPU_CONTAINERS = 10
"""Limit uncollectible interval overage for accounts without a saved card."""

NO_CARD_MAX_GPUS = 1

TEAM_PLAN_MONTHLY_NANOS = 49 * NANOS_PER_USD
"""The subscription, charged by the payment provider as a flat monthly price."""

TEAM_PLAN_INCLUDED_NANOS = 10 * NANOS_PER_USD
BUSINESS_PLAN_MONTHLY_NANOS = 249 * NANOS_PER_USD
BUSINESS_PLAN_INCLUDED_NANOS = 50 * NANOS_PER_USD


@dataclass(frozen=True, slots=True)
class SubscriptionTerms:
    version: SubscriptionTermsVersion
    plan: BillingPlanId
    monthly_nanos: int
    included_nanos: int


SUBSCRIPTION_TERMS: tuple[SubscriptionTerms, ...] = (
    SubscriptionTerms(
        SubscriptionTermsVersion.FreeLegacy,
        BillingPlanId.Free,
        0,
        5 * NANOS_PER_USD,
    ),
    SubscriptionTerms(
        SubscriptionTermsVersion.TeamLegacy,
        BillingPlanId.Team,
        100 * NANOS_PER_USD,
        30 * NANOS_PER_USD,
    ),
    SubscriptionTerms(
        SubscriptionTermsVersion.Free,
        BillingPlanId.Free,
        FREE_PLAN_MONTHLY_NANOS,
        FREE_PLAN_INCLUDED_NANOS,
    ),
    SubscriptionTerms(
        SubscriptionTermsVersion.Team,
        BillingPlanId.Team,
        TEAM_PLAN_MONTHLY_NANOS,
        TEAM_PLAN_INCLUDED_NANOS,
    ),
    SubscriptionTerms(
        SubscriptionTermsVersion.Business,
        BillingPlanId.Business,
        BUSINESS_PLAN_MONTHLY_NANOS,
        BUSINESS_PLAN_INCLUDED_NANOS,
    ),
)
_SUBSCRIPTION_TERMS_BY_VERSION = {terms.version: terms for terms in SUBSCRIPTION_TERMS}


def subscription_terms(version: SubscriptionTermsVersion) -> SubscriptionTerms:
    return _SUBSCRIPTION_TERMS_BY_VERSION[version]


_SECONDS_PER_HOUR = 3_600

SECONDS_PER_30_DAY_MONTH = 2_592_000
"""Storage uses a published 30-day month, independent of calendar length."""

CONNECTED_CLOUD_MANAGEMENT_FEE = Decimal("0.08")
"""Connected-cloud compute fee as a share of the equivalent fleet charge.

The customer pays their provider for capacity. Storage and egress use separate
published rates.
"""

STORED_RATE_STEP = Decimal("1E-12")
"""The smallest step the rate columns keep, which every stored rate lands on.

A hand-copy of their scale, because `shared` cannot import `database`. What holds
the two together is `tests/contracts`, which reads the column and compares."""


def _stored_rate(exact: Decimal) -> Decimal:
    """Round toward the customer without turning a positive price into free usage."""

    stored = exact.quantize(STORED_RATE_STEP, rounding=ROUND_DOWN)
    if stored == 0 and exact != 0:
        raise ValueError(
            f"{exact} is below the smallest rate {STORED_RATE_STEP} the rate column keeps; "
            "publishing it would charge nothing for a dimension the page prices"
        )
    return stored


def _management_fee(fleet_nanos_per_hour: int) -> int:
    """Round the management fee down to whole nanodollars per second."""

    fee = int(Decimal(fleet_nanos_per_hour) * CONNECTED_CLOUD_MANAGEMENT_FEE)
    return fee // _SECONDS_PER_HOUR * _SECONDS_PER_HOUR


def _per_second_rate(nanos_per_hour: int) -> Decimal:
    """Round down by less than one stored step per resource-second."""
    return _stored_rate(Decimal(nanos_per_hour) / _SECONDS_PER_HOUR)


@dataclass(frozen=True, slots=True)
class PublishedComputeRate:
    """Hourly resource prices converted to ledger precision without rounding up."""

    billing_owner: UsageBillingOwner
    gpu_type: str
    nanos_per_container_hour: int
    """What a container costs before any of its resources are counted."""

    nanos_per_cpu_core_hour: int
    nanos_per_memory_gib_hour: int
    nanos_per_gpu_card_hour: int
    rate_class: PlacementRateClass = AUTO_RATE_CLASS

    def __post_init__(self) -> None:
        """Reject unrepresentable rates when constructing the catalog."""

        _ = (
            self.nanos_per_container_second,
            self.nanos_per_cpu_core_second,
            self.nanos_per_memory_gib_second,
            self.nanos_per_gpu_card_second,
        )

    @property
    def nanos_per_container_second(self) -> Decimal:
        return _per_second_rate(self.nanos_per_container_hour)

    @property
    def nanos_per_cpu_core_second(self) -> Decimal:
        return _per_second_rate(self.nanos_per_cpu_core_hour)

    @property
    def nanos_per_memory_gib_second(self) -> Decimal:
        return _per_second_rate(self.nanos_per_memory_gib_hour)

    @property
    def nanos_per_gpu_card_second(self) -> Decimal:
        return _per_second_rate(self.nanos_per_gpu_card_hour)


@dataclass(frozen=True, slots=True)
class PublishedShapeRate:
    """What every container on one kind of capacity costs, before its GPU.

    The same three figures across every GPU model on that capacity, because a
    processor and a gibibyte cost what they cost whatever card sits beside them.
    Only the card's own rate varies, which is why the published card is these two
    small tables rather than one row per pair.
    """

    billing_owner: UsageBillingOwner
    nanos_per_container_hour: int
    nanos_per_cpu_core_hour: int
    nanos_per_memory_gib_hour: int


@dataclass(frozen=True, slots=True)
class PublishedGpuRate:
    """What one GPU model costs an hour, on each kind of capacity.

    One figure, because only the fleet's is a price this platform sets. What a
    card costs in a customer's own account is that figure times the management
    fee, and hardware they host themselves is free.
    """

    gpu_type: GpuType
    platform_fleet_nanos_per_card_hour: int

    def nanos_per_card_hour(self, billing_owner: UsageBillingOwner) -> int:
        """What one card of this model costs an hour on that kind of capacity.

        Every kind is answered explicitly and an unhandled one raises. A card
        given away by falling off the end of this would be a GPU rented for
        nothing, discovered by the invoice it never reached rather than by the
        capacity kind nobody priced.
        """

        if billing_owner is UsageBillingOwner.PlatformFleet:
            return self.platform_fleet_nanos_per_card_hour
        if billing_owner is UsageBillingOwner.ConnectedCloud:
            return _management_fee(self.platform_fleet_nanos_per_card_hour)
        if billing_owner is UsageBillingOwner.SelfHosted:
            # Hardware somebody brought, which this platform neither buys nor
            # manages: free by a published zero, the same way the shape rates
            # beside it are, rather than by a rate that is absent.
            return 0
        raise ValueError(f"no published GPU rate for capacity owned by {billing_owner}")


@dataclass(frozen=True, slots=True)
class PublishedPlatformRate:
    """Customer GiB prices converted to byte and byte-second ledger rates.

    Compute region selection does not change the customer egress rate.
    """

    nanos_per_egress_gib: int
    nanos_per_volume_gib_month: int

    def __post_init__(self) -> None:
        """Derive both rates once, for the same reason the compute rates do."""

        _ = (self.nanos_per_egress_byte, self.nanos_per_volume_byte_second)

    @property
    def nanos_per_egress_byte(self) -> Decimal:
        return _stored_rate(Decimal(self.nanos_per_egress_gib) / BYTES_PER_GIB)

    @property
    def nanos_per_volume_byte_second(self) -> Decimal:
        """A month here is thirty days, which is the unit the page states it in."""

        return _stored_rate(
            Decimal(self.nanos_per_volume_gib_month) / (BYTES_PER_GIB * SECONDS_PER_30_DAY_MONTH)
        )


@dataclass(frozen=True, slots=True)
class PublishedPlan:
    """Canonical plan prices, entitlements and copy for public clients."""

    id: BillingPlanId
    name: str
    """What this plan is called wherever a person is shown it."""

    summary: str
    """The one line under the name, saying what this plan is."""

    terms_version: SubscriptionTermsVersion
    entitlements: PlanEntitlements
    terms: tuple[str, ...]
    """Plan-specific promises, excluding numeric terms and platform-wide rules."""

    @property
    def monthly_nanos(self) -> int:
        return subscription_terms(self.terms_version).monthly_nanos

    @property
    def included_nanos(self) -> int:
        return subscription_terms(self.terms_version).included_nanos


@dataclass(frozen=True, slots=True)
class PlanEntitlements:
    """The limits and capabilities one plan grants to its account.

    Concurrency is two pools rather than one count with a GPU share inside it. A
    container counts against the CPU pool or, if it asks for cards, against the
    GPU pool by the number of cards, never both; so GPU work can never crowd out
    a customer's web apps and the pricing page can state each figure in one line.
    """

    max_concurrent_cpu_containers: int
    max_concurrent_gpus: int
    gpu_types: GpuTypeEntitlement
    max_workspaces: EntitlementLimit
    max_members: EntitlementLimit
    connected_cloud: bool
    custom_domains: bool
    self_hosted: bool
    log_retention_days: int
    region_selection: bool = False

    def __post_init__(self) -> None:
        if self.log_retention_days <= 0:
            raise ValueError("log retention must be positive")
        if self.max_concurrent_cpu_containers <= 0:
            raise ValueError("a plan must allow at least one concurrent CPU container")
        if self.max_concurrent_gpus <= 0:
            raise ValueError("a plan must allow at least one concurrent GPU")
        if self.gpu_types != "all" and not self.gpu_types:
            raise ValueError("a plan that names its GPU models must name at least one")
        if isinstance(self.max_workspaces, int) and self.max_workspaces <= 0:
            raise ValueError("a bounded workspace limit must be positive")
        if isinstance(self.max_members, int) and self.max_members <= 0:
            raise ValueError("a bounded member limit must be positive")

    def allows_gpu_type(self, gpu_type: GpuType) -> bool:
        return self.gpu_types == "all" or gpu_type in self.gpu_types

    @property
    def allowed_gpu_types(self) -> tuple[GpuType, ...]:
        """The models this plan may ask for, in the platform's published order.

        What a request for `any` card narrows to. Held to `SUPPORTED_GPU_TYPES`
        rather than to the plan's own set so the answer is always a model the
        scheduler can place, whichever way the set is written.
        """

        return tuple(model for model in SUPPORTED_GPU_TYPES if self.allows_gpu_type(model))


@dataclass(frozen=True, slots=True)
class AccountTerms:
    """Published recurring credits and the account's concurrency limits."""

    included_nanos: int
    entitlements: PlanEntitlements


def account_terms(plan: BillingPlanId, *, has_payment_method: bool) -> AccountTerms:
    """A saved card affects concurrency, never evidence that credits were funded."""

    published = published_plan(plan)
    if not has_payment_method:
        return AccountTerms(
            included_nanos=published.included_nanos,
            entitlements=replace(
                published.entitlements,
                max_concurrent_cpu_containers=NO_CARD_MAX_CPU_CONTAINERS,
                max_concurrent_gpus=NO_CARD_MAX_GPUS,
            ),
        )
    return AccountTerms(
        included_nanos=published.included_nanos,
        entitlements=published.entitlements,
    )


def complimentary_terms() -> AccountTerms:
    """What an account whose bill an administrator waived may run.

    The Team plan's own terms, as if a card were on file. Not a plan of its own,
    because a plan is something the provider prices and this card publishes, and
    a waiver is neither. The included figure is stated for completeness and
    decides nothing. Nothing such an account spends is owed, so there is no
    allowance to run out of. The concurrency ceiling still holds, since it bounds
    what the platform is exposed to rather than what anyone is billed.
    """

    return account_terms(BillingPlanId.Team, has_payment_method=True)


_PLATFORM_FLEET_SHAPE = PublishedShapeRate(
    UsageBillingOwner.PlatformFleet,
    0,
    55_126_800,
    7_560_000,
)
"""The original compute prices retained by historical publications."""

_INITIAL_SHAPE_RATES: tuple[PublishedShapeRate, ...] = (
    _PLATFORM_FLEET_SHAPE,
    PublishedShapeRate(
        UsageBillingOwner.ConnectedCloud,
        _management_fee(_PLATFORM_FLEET_SHAPE.nanos_per_container_hour),
        _management_fee(_PLATFORM_FLEET_SHAPE.nanos_per_cpu_core_hour),
        _management_fee(_PLATFORM_FLEET_SHAPE.nanos_per_memory_gib_hour),
    ),
    # Hardware somebody brought. Free by a published zero rather than by nothing
    # being written, so a self-hosted container still prices, still lands in the
    # ledger, and still shows up on the dashboard at $0.00.
    PublishedShapeRate(UsageBillingOwner.SelfHosted, 0, 0, 0),
)

_INITIAL_GPU_RATES: tuple[PublishedGpuRate, ...] = (
    PublishedGpuRate(GpuType.T4, 560_880_000),
    PublishedGpuRate(GpuType.A10G, 1_201_201_200),
    PublishedGpuRate(GpuType.L4, 899_398_800),
    PublishedGpuRate(GpuType.L40S, 2_138_346_000),
    PublishedGpuRate(GpuType.A100_40, 1_993_860_000),
    PublishedGpuRate(GpuType.A100_80, 2_925_626_400),
    PublishedGpuRate(GpuType.H100, 3_372_120_000),
    PublishedGpuRate(GpuType.H200, 3_918_236_400),
)
"""Original prices for every billable GPU identity, retained for historical usage."""

_SEPTEMBER_SHAPE_RATES = (
    PublishedShapeRate(UsageBillingOwner.PlatformFleet, 0, 22_000_000, 7_500_000),
    PublishedShapeRate(
        UsageBillingOwner.ConnectedCloud, 0, _management_fee(22_000_000), _management_fee(7_500_000)
    ),
    PublishedShapeRate(UsageBillingOwner.SelfHosted, 0, 0, 0),
)
_SEPTEMBER_GPU_RATES = tuple(
    replace(
        rate,
        platform_fleet_nanos_per_card_hour={
            GpuType.T4: 550_000_000,
            GpuType.A10G: 1_000_000_000,
            GpuType.L4: 750_000_000,
        }.get(rate.gpu_type, rate.platform_fleet_nanos_per_card_hour),
    )
    for rate in _INITIAL_GPU_RATES
)

_INITIAL_PLATFORM_RATE = PublishedPlatformRate(
    nanos_per_egress_gib=0,
    # Volumes are object storage, so this is priced against object storage rather
    # than against a block device: roughly twice what the bucket behind it lists
    # at, covering replication, the listing traffic the meter itself generates,
    # and the metering.
    nanos_per_volume_gib_month=50_000_000,
)

PUBLISHED_PLANS: tuple[PublishedPlan, ...] = (
    PublishedPlan(
        id=BillingPlanId.Free,
        name="Free",
        summary="What an account costs before it has agreed to anything.",
        terms_version=SubscriptionTermsVersion.Free,
        entitlements=PlanEntitlements(
            max_concurrent_cpu_containers=FREE_PLAN_MAX_CPU_CONTAINERS,
            max_concurrent_gpus=FREE_PLAN_MAX_GPUS,
            gpu_types=FREE_PLAN_GPU_TYPES,
            max_workspaces=FREE_PLAN_MAX_WORKSPACES,
            max_members=FREE_PLAN_MAX_MEMBERS,
            connected_cloud=False,
            custom_domains=False,
            self_hosted=True,
            log_retention_days=1,
        ),
        terms=(
            "Every workload the platform runs: applications, APIs, functions, jobs, "
            "queues, schedules, and sandboxes.",
            "No subscription to cancel and no minimum term.",
        ),
    ),
    PublishedPlan(
        id=BillingPlanId.Team,
        name="Team",
        summary="A monthly subscription with included usage credit.",
        terms_version=SubscriptionTermsVersion.Team,
        entitlements=PlanEntitlements(
            max_concurrent_cpu_containers=TEAM_PLAN_MAX_CPU_CONTAINERS,
            max_concurrent_gpus=TEAM_PLAN_MAX_GPUS,
            gpu_types="all",
            max_workspaces="unlimited",
            max_members="unlimited",
            connected_cloud=True,
            region_selection=True,
            custom_domains=True,
            self_hosted=True,
            log_retention_days=30,
        ),
        terms=(
            "The same workloads at the same metered rates, with higher account limits.",
            "Every GPU model the platform rents, and as many workspaces and members as you need.",
            "One account and invoice for every workspace it owns.",
        ),
    ),
    PublishedPlan(
        id=BillingPlanId.Business,
        name="Business",
        summary="Higher concurrency and longer log retention.",
        terms_version=SubscriptionTermsVersion.Business,
        entitlements=PlanEntitlements(
            max_concurrent_cpu_containers=2_000,
            max_concurrent_gpus=100,
            gpu_types="all",
            max_workspaces="unlimited",
            max_members="unlimited",
            connected_cloud=True,
            region_selection=True,
            custom_domains=True,
            self_hosted=True,
            log_retention_days=90,
        ),
        terms=("The same metered rates and capabilities as Team, with higher account limits.",),
    ),
)
"""Every plan an account can be on, cheapest first."""

_PLANS_BY_ID: Mapping[BillingPlanId, PublishedPlan] = {plan.id: plan for plan in PUBLISHED_PLANS}

if _PLANS_BY_ID.keys() != set(BillingPlanId):
    raise RuntimeError("every plan an account can be put on must publish its two figures")


def published_plan(plan: BillingPlanId) -> PublishedPlan:
    """What one plan charges and what it comes with.

    Total over the enum, held so by the check above, because the caller asking
    this is about to subscribe somebody: a plan with no figures behind it would
    be a subscription nobody can price.
    """

    return _PLANS_BY_ID[plan]


def _published_compute_rates(
    shapes: tuple[PublishedShapeRate, ...], gpus: tuple[PublishedGpuRate, ...]
) -> tuple[PublishedComputeRate, ...]:
    """One row per shape class the pricer can be asked for.

    CPU-only work is a shape class of its own rather than a missing GPU, so every
    (billing owner, GPU model) pair the scheduler can place has a rate and none of
    them falls through to an absent one.
    """

    rows: list[PublishedComputeRate] = []
    for shape in shapes:
        rows.append(
            PublishedComputeRate(
                billing_owner=shape.billing_owner,
                gpu_type=NO_GPU,
                nanos_per_container_hour=shape.nanos_per_container_hour,
                nanos_per_cpu_core_hour=shape.nanos_per_cpu_core_hour,
                nanos_per_memory_gib_hour=shape.nanos_per_memory_gib_hour,
                nanos_per_gpu_card_hour=0,
            )
        )
        rows.extend(
            PublishedComputeRate(
                billing_owner=shape.billing_owner,
                gpu_type=gpu.gpu_type.value,
                nanos_per_container_hour=shape.nanos_per_container_hour,
                nanos_per_cpu_core_hour=shape.nanos_per_cpu_core_hour,
                nanos_per_memory_gib_hour=shape.nanos_per_memory_gib_hour,
                nanos_per_gpu_card_hour=gpu.nanos_per_card_hour(shape.billing_owner),
            )
            for gpu in gpus
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class MeteredRateChange:
    """Changes only the listed compute classes and optional platform prices."""

    pricing_version: str
    effective_at: datetime
    compute_rates: tuple[PublishedComputeRate, ...]
    platform_rate: PublishedPlatformRate | None


@dataclass(frozen=True, slots=True)
class PublishedMeteredRateCard:
    pricing_version: str
    effective_at: datetime
    compute_rates: tuple[PublishedComputeRate, ...]
    platform_rate: PublishedPlatformRate


@dataclass(frozen=True, slots=True)
class PublishedPlacementRate:
    rate_class: PlacementRateClass
    pinned: bool
    preemptible: bool
    name: str
    cpu_memory_multiplier: Decimal
    gpu_multiplier: Decimal
    compute_rates: tuple[PublishedComputeRate, ...]


def _multiplied_hourly_rate(nanos_per_hour: int, multiplier: Decimal) -> int:
    amount = nanos_per_hour * multiplier
    if amount != amount.to_integral_value():
        raise ValueError("a multiplied hourly rate must remain an exact number of nanodollars")
    return int(amount)


def _placement_rates(
    rates: tuple[PublishedComputeRate, ...],
) -> tuple[PublishedPlacementRate, ...]:
    placements: list[PublishedPlacementRate] = []
    for pinned, preemptible, name in (
        (False, True, "Automatic"),
        (True, True, "Selected location"),
        (False, False, "Automatic, non-preemptible"),
        (True, False, "Selected location, non-preemptible"),
    ):
        rate_class = placement_rate_class(pinned=pinned, preemptible=preemptible)
        location = Decimal("1.5") if pinned else Decimal(1)
        cpu_memory = location * (1 if preemptible else 3)
        placements.append(
            PublishedPlacementRate(
                rate_class=rate_class,
                pinned=pinned,
                preemptible=preemptible,
                name=name,
                cpu_memory_multiplier=cpu_memory,
                gpu_multiplier=location,
                compute_rates=tuple(
                    replace(
                        rate,
                        rate_class=rate_class,
                        nanos_per_cpu_core_hour=_multiplied_hourly_rate(
                            rate.nanos_per_cpu_core_hour, cpu_memory
                        ),
                        nanos_per_memory_gib_hour=_multiplied_hourly_rate(
                            rate.nanos_per_memory_gib_hour, cpu_memory
                        ),
                        nanos_per_gpu_card_hour=_multiplied_hourly_rate(
                            rate.nanos_per_gpu_card_hour, location
                        ),
                    )
                    if rate.billing_owner is UsageBillingOwner.PlatformFleet
                    else replace(rate, rate_class=rate_class)
                    for rate in rates
                ),
            )
        )
    return tuple(placements)


PUBLISHED_METERED_RATE_HISTORY: tuple[MeteredRateChange, ...] = (
    MeteredRateChange(
        pricing_version="2026-08-18.a",
        effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        compute_rates=_published_compute_rates(_INITIAL_SHAPE_RATES, _INITIAL_GPU_RATES),
        platform_rate=_INITIAL_PLATFORM_RATE,
    ),
    MeteredRateChange(
        pricing_version="2026-09-09.a",
        effective_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        compute_rates=tuple(
            rate
            for placement in _placement_rates(
                _published_compute_rates(_INITIAL_SHAPE_RATES, _INITIAL_GPU_RATES)
            )
            if placement.rate_class != AUTO_RATE_CLASS
            for rate in placement.compute_rates
        ),
        platform_rate=None,
    ),
    MeteredRateChange(
        pricing_version="2026-09-04.a",
        effective_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
        compute_rates=_published_compute_rates(_INITIAL_SHAPE_RATES, _INITIAL_GPU_RATES),
        platform_rate=PublishedPlatformRate(
            nanos_per_egress_gib=130_000_000,
            nanos_per_volume_gib_month=50_000_000,
        ),
    ),
    MeteredRateChange(
        pricing_version="2026-09-12.a",
        effective_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        compute_rates=tuple(
            rate
            for placement in _placement_rates(
                _published_compute_rates(_SEPTEMBER_SHAPE_RATES, _SEPTEMBER_GPU_RATES)
            )
            for rate in placement.compute_rates
        ),
        platform_rate=None,
    ),
)
"""Reviewed price history. Existing cards retain their original figures and dates."""


def published_metered_rate_card(at: datetime) -> PublishedMeteredRateCard:
    applicable = tuple(
        card for card in PUBLISHED_METERED_RATE_HISTORY if card.effective_at <= to_utc(at)
    )
    if not applicable:
        raise ValueError("no published rates cover the requested time")
    latest = applicable[-1]
    return PublishedMeteredRateCard(
        pricing_version=latest.pricing_version,
        effective_at=latest.effective_at,
        compute_rates=tuple(
            {
                (rate.billing_owner, rate.rate_class, rate.gpu_type): rate
                for card in applicable
                for rate in card.compute_rates
            }.values()
        ),
        platform_rate=next(
            card.platform_rate for card in reversed(applicable) if card.platform_rate is not None
        ),
    )


METERED_RATE_VERSION = PUBLISHED_METERED_RATE_HISTORY[-1].pricing_version
METERED_RATES_EFFECTIVE_AT = PUBLISHED_METERED_RATE_HISTORY[-1].effective_at
_LATEST_METERED_CARD = published_metered_rate_card(METERED_RATES_EFFECTIVE_AT)
PUBLISHED_COMPUTE_RATES = _LATEST_METERED_CARD.compute_rates
PUBLISHED_PLATFORM_RATE = _LATEST_METERED_CARD.platform_rate
PUBLISHED_SHAPE_RATES = tuple(
    PublishedShapeRate(
        billing_owner=rate.billing_owner,
        nanos_per_container_hour=rate.nanos_per_container_hour,
        nanos_per_cpu_core_hour=rate.nanos_per_cpu_core_hour,
        nanos_per_memory_gib_hour=rate.nanos_per_memory_gib_hour,
    )
    for rate in PUBLISHED_COMPUTE_RATES
    if rate.rate_class == AUTO_RATE_CLASS and rate.gpu_type == NO_GPU
)
PUBLISHED_GPU_RATES = tuple(
    PublishedGpuRate(GpuType(rate.gpu_type), rate.nanos_per_gpu_card_hour)
    for rate in PUBLISHED_COMPUTE_RATES
    if rate.rate_class == AUTO_RATE_CLASS
    and rate.billing_owner is UsageBillingOwner.PlatformFleet
    and rate.gpu_type != NO_GPU
)


def published_placement_rates(
    rates: tuple[PublishedComputeRate, ...],
) -> tuple[PublishedPlacementRate, ...]:
    return tuple(
        replace(
            placement,
            compute_rates=tuple(rate for rate in rates if rate.rate_class == placement.rate_class),
        )
        for placement in _placement_rates(
            tuple(rate for rate in rates if rate.rate_class == AUTO_RATE_CLASS)
        )
        if any(rate.rate_class == placement.rate_class for rate in rates)
    )


if tuple(rate.gpu_type for rate in PUBLISHED_GPU_RATES) != SUPPORTED_GPU_TYPES:
    raise RuntimeError(
        "the published GPU rate card and the schedulable GPU list must name the same models"
    )


__all__ = [
    "BUSINESS_PLAN_INCLUDED_NANOS",
    "BUSINESS_PLAN_MONTHLY_NANOS",
    "CONNECTED_CLOUD_MANAGEMENT_FEE",
    "FREE_PLAN_GPU_TYPES",
    "FREE_PLAN_INCLUDED_NANOS",
    "FREE_PLAN_MAX_CPU_CONTAINERS",
    "FREE_PLAN_MAX_GPUS",
    "FREE_PLAN_MAX_MEMBERS",
    "FREE_PLAN_MAX_WORKSPACES",
    "FREE_PLAN_MONTHLY_NANOS",
    "METERED_RATES_EFFECTIVE_AT",
    "METERED_RATE_VERSION",
    "NO_CARD_MAX_CPU_CONTAINERS",
    "NO_CARD_MAX_GPUS",
    "ONE_TIME_TRIAL_NANOS",
    "PUBLISHED_COMPUTE_RATES",
    "PUBLISHED_GPU_RATES",
    "PUBLISHED_METERED_RATE_HISTORY",
    "PUBLISHED_PLANS",
    "PUBLISHED_PLATFORM_RATE",
    "PUBLISHED_SHAPE_RATES",
    "SECONDS_PER_30_DAY_MONTH",
    "STORED_RATE_STEP",
    "SUBSCRIPTION_TERMS",
    "TEAM_PLAN_INCLUDED_NANOS",
    "TEAM_PLAN_MAX_CPU_CONTAINERS",
    "TEAM_PLAN_MAX_GPUS",
    "TEAM_PLAN_MONTHLY_NANOS",
    "TRIAL_VALIDITY_DAYS",
    "AccountTerms",
    "AllGpuTypes",
    "EntitlementLimit",
    "GpuTypeEntitlement",
    "PlanEntitlements",
    "PublishedComputeRate",
    "PublishedGpuRate",
    "PublishedMeteredRateCard",
    "PublishedPlacementRate",
    "PublishedPlan",
    "PublishedPlatformRate",
    "PublishedShapeRate",
    "SubscriptionTerms",
    "account_terms",
    "complimentary_terms",
    "published_metered_rate_card",
    "published_placement_rates",
    "published_plan",
    "subscription_terms",
]
