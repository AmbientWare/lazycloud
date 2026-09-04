from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Literal, TypeAlias

from shared.billing_plans import BillingPlanId
from shared.billing_quotes import BYTES_PER_GIB, NANOS_PER_USD
from shared.gpu import NO_GPU, SUPPORTED_GPU_TYPES, GpuType
from shared.placement import AUTO_RATE_CLASS, PlacementRateClass, ProductRegion
from shared.usage import UsageBillingOwner

PRICING_VERSION = "2026-09-04.a"
"""The version of the plans, entitlements, and rates exposed to customers."""

FREE_PLAN_MONTHLY_NANOS = 0
"""What the free plan charges, published as a price rather than as no price.

A zero price is still a subscription line, and that is what carries the metered
prices an account's overage is billed through. An account on no subscription at
all would have nowhere for its usage to land.
"""

FREE_PLAN_INCLUDED_NANOS = 5 * NANOS_PER_USD
"""What the free plan comes with, issued as a credit grant each period."""

FREE_PLAN_MAX_CPU_CONTAINERS = 30
"""How much the free plan may run at once without a GPU, across every workspace.

A term of the plan rather than a scheduler setting, because it is part of what an
account is buying and the pricing page states it. Counted per account and not per
workspace, because the count is a bound on one payer's blast radius and a payer
is the unit that gets billed for it.
"""

FREE_PLAN_MAX_GPUS = 5
"""How many GPU cards the free plan may hold at once, across every workspace.

A pool of its own rather than a share of the container count, because the two
limits bound different things. A CPU container costs cents an hour on capacity
that is cheap to keep warm; a card costs dollars an hour on hardware that is not,
so the same number cannot be both a generous CPU ceiling and a sane GPU one.
Counted in cards rather than containers because a container may ask for several
and cards are what is scarce.
"""

FREE_PLAN_GPU_TYPES: frozenset[GpuType] = frozenset({GpuType.T4, GpuType.L4, GpuType.A10G})
"""Which cards the free plan may ask for: the ones renting for around a dollar an hour.

Not a revenue gate. A free account with a card pays the metered rate on any
model, so what this protects is the larger cards themselves, which are the ones
expensive to hold idle and the ones an abuser wants most.
"""

FREE_PLAN_MAX_WORKSPACES = 1
FREE_PLAN_MAX_MEMBERS = 1
"""The owner, and nobody else.

Counted the way the membership repository counts, which includes the owner's own
membership row, so one is a workspace with no co-members rather than no
workspace at all. Wanting to work with somebody is the free plan's upgrade
trigger, and it is the one every customer understands without reading terms.
"""

TEAM_PLAN_MAX_CPU_CONTAINERS = 1_000
TEAM_PLAN_MAX_GPUS = 50

UnlimitedEntitlement: TypeAlias = Literal["unlimited"]
EntitlementLimit: TypeAlias = int | UnlimitedEntitlement

AllGpuTypes: TypeAlias = Literal["all"]
GpuTypeEntitlement: TypeAlias = frozenset[GpuType] | AllGpuTypes
"""Which cards a plan may ask for: a named set, or every model the platform rents."""

NO_CARD_INCLUDED_NANOS = 1 * NANOS_PER_USD
"""What an account with no card on file may spend before it is stopped.

Enough to run something real and see it work, and small enough that losing all of
it costs less than the sign-up did. Nothing here can be collected — there is no
payment method to charge — so this figure is spending, not credit.

It is not the free plan's allowance reduced. The free plan's $5 is what an
account gets once somebody can be billed for what they do next; this is what the
platform is willing to give away to find that out.
"""

NO_CARD_MAX_CPU_CONTAINERS = 10
"""How much an account with no card may run at once without a GPU.

The real bound on what a cardless account can spend before anything stops it, and
the reason it is far below the free plan's. Usage reaches the ledger on an
interval, so an account is always some fraction of that interval past whatever it
has been measured at; multiply that window by the burn rate of everything running
and the product is what cannot be collected. This is the only term in it the
platform sets directly.
"""

NO_CARD_MAX_GPUS = 1
"""One card, because the spending cap above stops it inside an hour on any model
the free plan may ask for, and none is an account that can never see a GPU work."""

TEAM_PLAN_MONTHLY_NANOS = 100 * NANOS_PER_USD
"""The subscription, charged by the payment provider as a flat monthly price."""

TEAM_PLAN_INCLUDED_NANOS = 30 * NANOS_PER_USD
"""What the subscription comes with, issued as a credit grant each period.

Stated in nanodollars like every other figure here; the provider's grant is in
cents, and 30 USD converts exactly.
"""

_SECONDS_PER_HOUR = 3_600
"""Whole, not a `Decimal`: it is a divisor and a modulus, never a price."""

SECONDS_PER_30_DAY_MONTH = 2_592_000
"""The month volume storage is quoted by, said in seconds because that is what a
byte-second rate is derived against. Thirty days, which the page states outright
rather than leaving a reader to assume their own calendar month."""

CONNECTED_CLOUD_MANAGEMENT_FEE = Decimal("0.08")
"""What this platform charges to run a container on capacity somebody else pays for.

A share of what the same container would cost on the fleet, rather than a price
of its own. Their cloud bills them for the machine; this is the fee for placing,
scheduling, supervising and metering what runs on it, so it is the one figure
that decides every connected-cloud compute rate and there is no second table to
keep in step with the first.

Compute only. Volumes live in this platform's own object store and egress is
measured here, so both are charged whole wherever the container ran — a share of
a bill this platform is paying itself would be selling storage below cost.
"""

STORED_RATE_STEP = Decimal("1E-12")
"""The smallest step the rate columns keep, which every stored rate lands on.

A hand-copy of their scale, because `shared` cannot import `database`. What holds
the two together is `tests/contracts`, which reads the column and compares."""


def _stored_rate(exact: Decimal) -> Decimal:
    """A rate the column holds exactly, at or below the figure it came from.

    A price per gibibyte-month has no exact rate per byte-second: the divisor
    carries factors of three, so the quotient does not terminate and no amount of
    column precision would make it. Compute never meets this because a per-hour
    figure divides by 3600 into a whole nanodollar, which the card holds as a
    term rather than a coincidence.

    So the published figure is the one this card owns and the stored rate is
    derived from it downwards. The direction is the whole of it. Rounding up
    would charge fractionally more than the page states, which is a price the
    platform never published; rounding down charges fractionally less, which is a
    rounding artefact in the customer's favour and costs eighteen millionths of
    the bill.

    Down has a floor, and reaching it is refused rather than rounded to. A price
    small enough to land under the column's last step would be stored as zero,
    and a zero here is indistinguishable from the stated zero that means free —
    so the page would publish a price and the platform would bill nothing at all,
    which no comparison against the published figure can catch because charging
    nothing is charging no more than it says.
    """

    stored = exact.quantize(STORED_RATE_STEP, rounding=ROUND_DOWN)
    if stored == 0 and exact != 0:
        raise ValueError(
            f"{exact} is below the smallest rate {STORED_RATE_STEP} the rate column keeps; "
            "publishing it would charge nothing for a dimension the page prices"
        )
    return stored


def _management_fee(fleet_nanos_per_hour: int) -> int:
    """The fleet's hourly price as the fee for running the same thing elsewhere.

    Snapped down to a whole nanodollar a second, because a share of a price is
    not generally divisible by 3600 and the card refuses a figure that is not.
    Down rather than nearest, for the reason `_stored_rate` rounds down: the
    published figure is what a customer is quoted, and landing under it is a
    rounding artefact where landing over it is a price nobody published. The
    snap costs at most two thousandths of a percent.
    """

    fee = int(Decimal(fleet_nanos_per_hour) * CONNECTED_CLOUD_MANAGEMENT_FEE)
    return fee // _SECONDS_PER_HOUR * _SECONDS_PER_HOUR


def _exact_per_second(nanos_per_hour: int) -> Decimal:
    """An hourly price as the per-second rate the ledger multiplies, or nothing.

    Compute refuses where the platform rates round: an hourly figure is chosen by
    whoever prices the card, so one that does not divide into a whole nanodollar
    a second is a figure to correct rather than a quotient to truncate. Rounding
    it down here would quietly sell a processor for less than the page says
    forever, and letting it through unrounded hands the decision to the rate
    column, which may round it up.

    Whole nanodollars, not merely a figure the column can hold. Those are
    different tests and the weaker one is not enough: 55_126_809 an hour is
    15313.0025 a second, which `NUMERIC(30, 12)` stores exactly and the pricing
    page still refuses, because per-second is a unit the page publishes rather
    than only a number the database keeps.
    """

    if nanos_per_hour % _SECONDS_PER_HOUR != 0:
        raise ValueError(
            f"{nanos_per_hour} nanodollars an hour is not a whole number of nanodollars a "
            f"second; every published figure divides by {_SECONDS_PER_HOUR}, and one "
            "that does not has no per-second price to publish"
        )
    return Decimal(nanos_per_hour) / _SECONDS_PER_HOUR


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
    divisibility is load-bearing rather than incidental, and `_exact_per_second`
    holds it here rather than leaving it to the contract test: a price that broke
    it would otherwise reach the rate column as a 28-digit quotient and be
    rounded by the database — in whichever direction the database chose, which
    may be upward, and a rate above the published figure is a price the platform
    never stated.
    """

    billing_owner: UsageBillingOwner
    gpu_type: str
    nanos_per_container_hour: int
    """What a container costs before any of its resources are counted."""

    nanos_per_cpu_core_hour: int
    nanos_per_memory_gib_hour: int
    nanos_per_gpu_card_hour: int
    rate_class: PlacementRateClass = AUTO_RATE_CLASS

    def __post_init__(self) -> None:
        """Derive every figure once, so an unpublishable one raises on construction.

        The properties below are lazy, and their only production reader is the
        command that writes the rate rows. Left to them, a price nobody can
        publish would be discovered by an operator mid-cutover rather than by the
        import that builds this card — which is every consumer, and every test.
        """

        _ = (
            self.nanos_per_container_second,
            self.nanos_per_cpu_core_second,
            self.nanos_per_memory_gib_second,
            self.nanos_per_gpu_card_second,
        )

    @property
    def nanos_per_container_second(self) -> Decimal:
        return _exact_per_second(self.nanos_per_container_hour)

    @property
    def nanos_per_cpu_core_second(self) -> Decimal:
        return _exact_per_second(self.nanos_per_cpu_core_hour)

    @property
    def nanos_per_memory_gib_second(self) -> Decimal:
        return _exact_per_second(self.nanos_per_memory_gib_hour)

    @property
    def nanos_per_gpu_card_second(self) -> Decimal:
        return _exact_per_second(self.nanos_per_gpu_card_hour)


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
    """What the platform charges for what a container moves and keeps.

    Held in the whole units a customer compares — a gibibyte moved, a gibibyte
    kept for a thirty-day month — and divided below into the per-byte and
    per-byte-second rates the ledger prices against. That direction is the same
    one the compute rates take, and for the same reason: the published figure is
    what a person is quoted, so it is the figure this card states rather than one
    reconstructed from a rate row.

    Egress is global. Selecting a compute region does not change the customer
    transfer rate.
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
    """Everything a surface offering this plan states about it.

    The figures and the words together, because a plan described in one place and
    priced in another is a plan somebody ships half of. Adding one here is what
    makes it appear on the pricing page and in the dashboard; neither has copy of
    its own to keep in step.
    """

    id: BillingPlanId
    name: str
    """What this plan is called wherever a person is shown it."""

    summary: str
    """The one line under the name, saying what this plan is."""

    monthly_nanos: int
    included_nanos: int
    entitlements: PlanEntitlements
    terms: tuple[str, ...]
    """What this plan promises beyond its figures, one clause each.

    Carries no money and no count. The figures above are rendered by whichever
    surface shows the plan, in that surface's own format, so a term can never
    restate a number and then disagree with it. What is true on every plan —
    what a card on file changes, how included compute is issued, how overage is
    billed — is not here either: it belongs to the surface that says it once,
    and repeating it per plan is how two plans start describing the platform
    differently.
    """


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
    region_selection: bool = False

    def __post_init__(self) -> None:
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
    """What one account may spend and run for a cycle.

    A plan's figures are not an account's. Everything a plan comes with is
    promised against being able to charge for what happens next, and an account
    with no card on file has not made that possible — so the two figures move
    together, from the same fact, and are resolved in one place rather than by
    two callers each deciding what a missing card means.
    """

    included_nanos: int
    entitlements: PlanEntitlements


def account_terms(plan: BillingPlanId, *, has_payment_method: bool) -> AccountTerms:
    """What this account gets, given its plan and whether anyone can charge it.

    Having no card replaces the plan's terms rather than reducing them, and it
    does so whatever the plan says. A subscription nobody can collect on is not a
    cheaper subscription — the $200 plan's invoice fails exactly like the free
    one's — so there is no plan for which "they have no card" should still mean
    "give them the plan's allowance".

    The plan still decides everything once a card exists, which is the only state
    a paid plan is ever meant to be in.
    """

    published = published_plan(plan)
    if not has_payment_method:
        return AccountTerms(
            included_nanos=NO_CARD_INCLUDED_NANOS,
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
"""The one compute price this platform sets. Every other capacity derives from it."""

_INITIAL_SHAPE_RATES: tuple[PublishedShapeRate, ...] = (
    _PLATFORM_FLEET_SHAPE,
    # Capacity in a customer's own cloud account: their provider bills them for
    # the machine, so what this platform charges is the fee for managing what
    # was placed there — the fleet's own price, shared.
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
"""Every GPU model the platform schedules, at the price it is rented for.

Held to `shared.gpu.SUPPORTED_GPU_TYPES` exactly: a model that schedules and has
no row here is compute nothing can price, and a row for a model nobody can rent
is a quote nobody can take.
"""

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
        monthly_nanos=FREE_PLAN_MONTHLY_NANOS,
        included_nanos=FREE_PLAN_INCLUDED_NANOS,
        entitlements=PlanEntitlements(
            max_concurrent_cpu_containers=FREE_PLAN_MAX_CPU_CONTAINERS,
            max_concurrent_gpus=FREE_PLAN_MAX_GPUS,
            gpu_types=FREE_PLAN_GPU_TYPES,
            max_workspaces=FREE_PLAN_MAX_WORKSPACES,
            max_members=FREE_PLAN_MAX_MEMBERS,
            connected_cloud=False,
            custom_domains=False,
            self_hosted=True,
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
        summary="A monthly subscription that comes with compute included.",
        monthly_nanos=TEAM_PLAN_MONTHLY_NANOS,
        included_nanos=TEAM_PLAN_INCLUDED_NANOS,
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
        ),
        terms=(
            "The same workloads at the same metered rates, with higher account limits.",
            "Every GPU model the platform rents, and as many workspaces and members as you need.",
            "One account and invoice for every workspace it owns.",
        ),
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
class PublishedMeteredRateCard:
    pricing_version: str
    effective_at: datetime
    compute_rates: tuple[PublishedComputeRate, ...]
    platform_rate: PublishedPlatformRate


PUBLISHED_METERED_RATE_HISTORY: tuple[PublishedMeteredRateCard, ...] = (
    PublishedMeteredRateCard(
        pricing_version="2026-08-18.a",
        effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        compute_rates=_published_compute_rates(_INITIAL_SHAPE_RATES, _INITIAL_GPU_RATES),
        platform_rate=_INITIAL_PLATFORM_RATE,
    ),
    PublishedMeteredRateCard(
        pricing_version="2026-09-04.a",
        effective_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
        compute_rates=_published_compute_rates(_INITIAL_SHAPE_RATES, _INITIAL_GPU_RATES),
        platform_rate=PublishedPlatformRate(
            nanos_per_egress_gib=130_000_000,
            nanos_per_volume_gib_month=50_000_000,
        ),
    ),
)
"""Reviewed price history. Existing cards retain their original figures and dates."""

_CURRENT_METERED_CARD = PUBLISHED_METERED_RATE_HISTORY[-1]
METERED_RATE_VERSION = _CURRENT_METERED_CARD.pricing_version
METERED_RATES_EFFECTIVE_AT = _CURRENT_METERED_CARD.effective_at
PUBLISHED_COMPUTE_RATES = _CURRENT_METERED_CARD.compute_rates
PUBLISHED_PLATFORM_RATE = _CURRENT_METERED_CARD.platform_rate
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


@dataclass(frozen=True, slots=True)
class PublishedPlacementRate:
    rate_class: PlacementRateClass
    region: ProductRegion | None
    name: str
    multiplier: Decimal
    compute_rates: tuple[PublishedComputeRate, ...]

    def __post_init__(self) -> None:
        if self.multiplier <= 0 or not self.compute_rates:
            raise ValueError("a placement class requires a positive multiplier and compute rates")
        if any(rate.rate_class != self.rate_class for rate in self.compute_rates):
            raise ValueError("placement compute rates must belong to their published class")


PUBLISHED_PLACEMENT_RATES: tuple[PublishedPlacementRate, ...] = (
    PublishedPlacementRate(
        rate_class=AUTO_RATE_CLASS,
        region=None,
        name="Automatic",
        multiplier=Decimal(1),
        compute_rates=PUBLISHED_COMPUTE_RATES,
    ),
)


def published_placement_rate(region: ProductRegion | None) -> PublishedPlacementRate | None:
    return next((rate for rate in PUBLISHED_PLACEMENT_RATES if rate.region == region), None)


if tuple(rate.gpu_type for rate in PUBLISHED_GPU_RATES) != SUPPORTED_GPU_TYPES:
    raise RuntimeError(
        "the published GPU rate card and the schedulable GPU list must name the same models"
    )


__all__ = [
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
    "NO_CARD_INCLUDED_NANOS",
    "NO_CARD_MAX_CPU_CONTAINERS",
    "NO_CARD_MAX_GPUS",
    "PRICING_VERSION",
    "PUBLISHED_COMPUTE_RATES",
    "PUBLISHED_GPU_RATES",
    "PUBLISHED_METERED_RATE_HISTORY",
    "PUBLISHED_PLACEMENT_RATES",
    "PUBLISHED_PLANS",
    "PUBLISHED_PLATFORM_RATE",
    "PUBLISHED_SHAPE_RATES",
    "SECONDS_PER_30_DAY_MONTH",
    "STORED_RATE_STEP",
    "TEAM_PLAN_INCLUDED_NANOS",
    "TEAM_PLAN_MAX_CPU_CONTAINERS",
    "TEAM_PLAN_MAX_GPUS",
    "TEAM_PLAN_MONTHLY_NANOS",
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
    "account_terms",
    "complimentary_terms",
    "published_placement_rate",
    "published_plan",
]
