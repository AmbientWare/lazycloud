from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from shared.billing_plans import BillingPlanId
from shared.billing_quotes import NANOS_PER_USD
from shared.gpu import NO_GPU, SUPPORTED_GPU_TYPES, GpuType
from shared.usage import UsageBillingOwner

PRICING_VERSION = "2026-08-13.a"
"""The label frozen onto every ledger segment these numbers price.

Opaque and unparsed. It exists so "which numbers produced this charge" is
answerable from one column, which means it moves whenever any figure below does.
"""

RATES_EFFECTIVE_ON = date(2026, 8, 13)
"""The day this card takes effect, as the pricing page states it.

The instant a rate row carries is supplied at publish time and must be in the
future; this is the day the owner published it for, and the one figure the page
and the card have to agree on beyond the money itself.
"""

FREE_PLAN_MONTHLY_NANOS = 0
"""What the free plan charges, published as a price rather than as no price.

A zero price is still a subscription line, and that is what carries the metered
prices an account's overage is billed through. An account on no subscription at
all would have nowhere for its usage to land.
"""

FREE_PLAN_INCLUDED_NANOS = 5 * NANOS_PER_USD
"""What the free plan comes with, issued as a credit grant each period."""

TEAM_PLAN_MONTHLY_NANOS = 200 * NANOS_PER_USD
"""The subscription, charged by the payment provider as a flat monthly price."""

TEAM_PLAN_INCLUDED_NANOS = 100 * NANOS_PER_USD
"""What the subscription comes with, issued as a credit grant each period.

Stated in nanodollars like every other figure here; the provider's grant is in
cents, and 100 USD converts exactly.
"""

_SECONDS_PER_HOUR = Decimal(3_600)
_BYTES_PER_GIB = Decimal(1_073_741_824)
_SECONDS_PER_30_DAY_MONTH = Decimal(2_592_000)


@dataclass(frozen=True, slots=True)
class PublishedComputeRate:
    """One shape class's figures, in the units they are published in.

    Published per hour and per whole unit — a core, a gibibyte, a card — because
    that is what a customer compares against every other cloud. The database
    stores the per-second figures derived below, and the derivation is exact
    division rather than a rounded conversion, so the figure on the page and the
    figure in the rate row are the same number said twice.

    Every figure on this card divides by 3600 to a whole nanodollar, which is
    what lets a per-second rate reach `NUMERIC(30, 12)` unrounded. That
    divisibility is load-bearing rather than incidental: a new price or a new GPU
    model that does not have it would be stored rounded and billed at a rate the
    page never stated, and `tests/contracts/test_published_prices_match_their_owners`
    is what says so.
    """

    billing_owner: UsageBillingOwner
    gpu_type: str
    nanos_per_container_hour: int
    """What a container costs before any of its resources are counted."""

    nanos_per_cpu_core_hour: int
    nanos_per_memory_gib_hour: int
    nanos_per_gpu_card_hour: int

    @property
    def nanos_per_container_second(self) -> Decimal:
        return Decimal(self.nanos_per_container_hour) / _SECONDS_PER_HOUR

    @property
    def nanos_per_cpu_core_second(self) -> Decimal:
        return Decimal(self.nanos_per_cpu_core_hour) / _SECONDS_PER_HOUR

    @property
    def nanos_per_memory_gib_second(self) -> Decimal:
        return Decimal(self.nanos_per_memory_gib_hour) / _SECONDS_PER_HOUR

    @property
    def nanos_per_gpu_card_second(self) -> Decimal:
        return Decimal(self.nanos_per_gpu_card_hour) / _SECONDS_PER_HOUR


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
    """What one GPU model costs an hour, on each kind of capacity."""

    gpu_type: GpuType
    platform_fleet_nanos_per_card_hour: int
    connected_cloud_nanos_per_card_hour: int

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
            return self.connected_cloud_nanos_per_card_hour
        if billing_owner is UsageBillingOwner.SelfHosted:
            # Hardware somebody brought, which this platform neither buys nor
            # manages: free by a published zero, the same way the shape rates
            # beside it are, rather than by a rate that is absent.
            return 0
        raise ValueError(f"no published GPU rate for capacity owned by {billing_owner}")


@dataclass(frozen=True, slots=True)
class PublishedPlatformRate:
    """What the platform charges for what a container moves and keeps.

    Both figures are a stated zero rather than an absent one. The metering runs,
    the ledger records the segments, the meter events leave for the provider, and
    the invoice carries a line reading $0.00 — so turning either on later is one
    published rate row and no change to any of that.

    Held per byte and per byte-second, which is what the ledger prices against,
    and derived below into the whole units a customer compares — a gibibyte, a
    gibibyte held for a month. Exact multiplication rather than a rounded
    conversion, so the figure on the page and the figure in the rate row are the
    same number said twice.
    """

    nanos_per_egress_byte: Decimal
    nanos_per_volume_byte_second: Decimal

    @property
    def nanos_per_egress_gib(self) -> Decimal:
        return self.nanos_per_egress_byte * _BYTES_PER_GIB

    @property
    def nanos_per_volume_gib_month(self) -> Decimal:
        """A month here is thirty days, which is the unit the page states it in."""

        return self.nanos_per_volume_byte_second * _BYTES_PER_GIB * _SECONDS_PER_30_DAY_MONTH


@dataclass(frozen=True, slots=True)
class PublishedPlan:
    """What one plan charges and what it comes with.

    The two figures only. What a plan is called and how it is described is the
    pricing page's, and a plan is not a rate: `id` is what joins the two.
    """

    id: BillingPlanId
    monthly_nanos: int
    included_nanos: int


PUBLISHED_SHAPE_RATES: tuple[PublishedShapeRate, ...] = (
    PublishedShapeRate(UsageBillingOwner.PlatformFleet, 0, 55_126_800, 7_560_000),
    # Capacity in a customer's own cloud account: their provider bills them for
    # the machine, so this is the fee on what was placed there.
    PublishedShapeRate(UsageBillingOwner.ConnectedCloud, 0, 2_854_800, 273_600),
    # Hardware somebody brought. Free by a published zero rather than by nothing
    # being written, so a self-hosted container still prices, still lands in the
    # ledger, and still shows up on the dashboard at $0.00.
    PublishedShapeRate(UsageBillingOwner.SelfHosted, 0, 0, 0),
)

PUBLISHED_GPU_RATES: tuple[PublishedGpuRate, ...] = (
    PublishedGpuRate(GpuType.T4, 560_880_000, 26_280_000),
    PublishedGpuRate(GpuType.A10G, 1_201_201_200, 64_681_200),
    PublishedGpuRate(GpuType.L4, 899_398_800, 48_585_600),
    PublishedGpuRate(GpuType.L40S, 2_138_346_000, 128_703_600),
    PublishedGpuRate(GpuType.A100_40, 1_993_860_000, 254_001_600),
    PublishedGpuRate(GpuType.A100_80, 2_925_626_400, 335_998_800),
    PublishedGpuRate(GpuType.H100, 3_372_120_000, 844_801_200),
    PublishedGpuRate(GpuType.H200, 3_918_236_400, 1_000_800_000),
)
"""Every GPU model the platform schedules, at the price it is rented for.

Held to `shared.gpu.SUPPORTED_GPU_TYPES` exactly: a model that schedules and has
no row here is compute nothing can price, and a row for a model nobody can rent
is a quote nobody can take.
"""

PUBLISHED_PLATFORM_RATE = PublishedPlatformRate(
    nanos_per_egress_byte=Decimal(0),
    nanos_per_volume_byte_second=Decimal(0),
)

PUBLISHED_PLANS: tuple[PublishedPlan, ...] = (
    PublishedPlan(
        id=BillingPlanId.Free,
        monthly_nanos=FREE_PLAN_MONTHLY_NANOS,
        included_nanos=FREE_PLAN_INCLUDED_NANOS,
    ),
    PublishedPlan(
        id=BillingPlanId.Team,
        monthly_nanos=TEAM_PLAN_MONTHLY_NANOS,
        included_nanos=TEAM_PLAN_INCLUDED_NANOS,
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


def _published_compute_rates() -> tuple[PublishedComputeRate, ...]:
    """One row per shape class the pricer can be asked for.

    CPU-only work is a shape class of its own rather than a missing GPU, so every
    (billing owner, GPU model) pair the scheduler can place has a rate and none of
    them falls through to an absent one.
    """

    rows: list[PublishedComputeRate] = []
    for shape in PUBLISHED_SHAPE_RATES:
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
            for gpu in PUBLISHED_GPU_RATES
        )
    return tuple(rows)


PUBLISHED_COMPUTE_RATES: tuple[PublishedComputeRate, ...] = _published_compute_rates()
"""The whole compute rate card, expanded to the rows the database holds."""

if tuple(rate.gpu_type for rate in PUBLISHED_GPU_RATES) != SUPPORTED_GPU_TYPES:
    raise RuntimeError(
        "the published GPU rate card and the schedulable GPU list must name the same models"
    )


__all__ = [
    "FREE_PLAN_INCLUDED_NANOS",
    "FREE_PLAN_MONTHLY_NANOS",
    "PRICING_VERSION",
    "PUBLISHED_COMPUTE_RATES",
    "PUBLISHED_GPU_RATES",
    "PUBLISHED_PLANS",
    "PUBLISHED_PLATFORM_RATE",
    "PUBLISHED_SHAPE_RATES",
    "RATES_EFFECTIVE_ON",
    "TEAM_PLAN_INCLUDED_NANOS",
    "TEAM_PLAN_MONTHLY_NANOS",
    "PublishedComputeRate",
    "PublishedGpuRate",
    "PublishedPlan",
    "PublishedPlatformRate",
    "PublishedShapeRate",
    "published_plan",
]
