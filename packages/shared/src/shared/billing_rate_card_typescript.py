"""The published rate card, rendered as the TypeScript the pricing page compiles.

The marketing pages render without calling the API, so the card has to exist in
the browser bundle. Written by hand that is a transcription, and a transcription
of a price is the one kind of drift a customer discovers by being charged
something the published page did not say — so it is generated from the card
instead, and the generated file is checked rather than trusted.

Only what the card owns is rendered, and only what a page reads: the figures, the
owners, the models, and every plan whole — name, summary, figures, and terms. The
page joins nothing to it, which is what makes adding a plan one edit rather than
two that drift.

The rates the ledger prices against are not here. A browser has nothing to do
with a figure per byte-second, and rendering one into the bundle is a second
place a price could be read from — a place where it would be read rounded,
because the published figure it derives from is the exact one.
"""

from __future__ import annotations

from shared.billing_rate_card import (
    CONNECTED_CLOUD_MANAGEMENT_FEE,
    NO_CARD_INCLUDED_NANOS,
    NO_CARD_MAX_CONTAINERS,
    PUBLISHED_GPU_RATES,
    PUBLISHED_PLANS,
    PUBLISHED_PLATFORM_RATE,
    PUBLISHED_SHAPE_RATES,
    PublishedGpuRate,
    PublishedPlan,
)

REGENERATE_COMMAND = (
    "uv run lazycloud-admin billing write-pricing-catalog "
    "--output apps/web/src/routes/-marketing/pricingCatalog.ts"
)
"""How a reader of the generated file makes it current again.

Named in the file itself and in the failure of the check that compares them,
because a generated artifact whose regeneration has to be looked up is one people
edit by hand instead.
"""


def render_pricing_catalog() -> str:
    """The whole generated module, ending in the newline a file ends with."""

    blocks = (
        _header(),
        _billing_owners(),
        _shape_rate_type(),
        _shape_rates(),
        _gpu_rate_type(),
        _gpu_rates(),
        _platform_rates(),
        _plan_ids(),
        _plan_type(),
        _plans(),
        _no_card_terms(),
    )
    return "\n\n".join(blocks) + "\n"


def _header() -> str:
    return f"""/* Generated from `shared.billing_rate_card`. Do not edit.

   Regenerate with:
     {REGENERATE_COMMAND}

   That card is also what `lazycloud-admin billing publish-rates` writes into the
   rate tables the pricer quotes from, so the figures below and the figures a
   customer is charged have one owner. The pricing page renders without calling
   the API, which is why they are compiled in rather than fetched, and
   `tests/contracts` fails while this file and the card disagree — an edit made
   here instead of there is lost at the next regeneration and would have quoted a
   price the platform never held.

   Figures are nanodollars — billionths of a dollar — per whole unit of whatever
   the line is sold by: an hour of a container, a core, a gibibyte of memory, a
   card; a gibibyte moved; a gibibyte kept for a thirty-day month. Every one is a
   whole number, so the page renders a published price rather than a rounded
   one. */"""


def _billing_owners() -> str:
    owners = ", ".join(_string(shape.billing_owner.value) for shape in PUBLISHED_SHAPE_RATES)
    return (
        f"export const billingOwners = [{owners}] as const;\n"
        "export type BillingOwner = (typeof billingOwners)[number];"
    )


def _shape_rate_type() -> str:
    """What a container's resources cost, which is what the page has lines for.

    The card also prices the container itself, before any of its resources. That
    figure is not rendered: the page states resources, `tests/contracts` holds it
    at zero, and emitting a column no line reads would be a price in the bundle
    that nothing could show.
    """

    return """export type PublishedShapeRate = {
  nanosPerCpuCoreHour: number;
  nanosPerMemoryGibHour: number;
};"""


def _shape_rates() -> str:
    entries = "".join(
        f"  {shape.billing_owner.value}: {{\n"
        f"    nanosPerCpuCoreHour: {_integer(shape.nanos_per_cpu_core_hour)},\n"
        f"    nanosPerMemoryGibHour: {_integer(shape.nanos_per_memory_gib_hour)},\n"
        "  },\n"
        for shape in PUBLISHED_SHAPE_RATES
    )
    return (
        "/* Every container on one kind of capacity pays these three, whatever card sits\n"
        "   beside it. Only the card's own rate varies, which is why the card is two\n"
        "   small tables rather than one row per pair. */\n"
        "export const publishedShapeRates = {\n"
        f"{entries}"
        "} as const satisfies Record<BillingOwner, PublishedShapeRate>;"
    )


def _gpu_rate_type() -> str:
    return """export type PublishedGpuRate = {
  gpuType: string;
  /** What one card of this model costs an hour, on each kind of capacity. */
  nanosPerCardHour: Record<BillingOwner, number>;
};"""


def _gpu_rates() -> str:
    entries = "".join(_gpu_rate(rate) for rate in _most_expensive_first(PUBLISHED_GPU_RATES))
    return (
        "/* Most expensive first on LazyCloud capacity. The capacities rank differently,\n"
        "   so one published order has to lead. */\n"
        "export const publishedGpuRates = [\n"
        f"{entries}"
        "] as const satisfies readonly PublishedGpuRate[];"
    )


def _gpu_rate(rate: PublishedGpuRate) -> str:
    """One model, priced on every capacity the card publishes shape rates for.

    Keyed by owner rather than by two named fields, so the page reads a card's
    price the same way it reads a core's and a capacity added to the card is one
    a consumer cannot leave unpriced.
    """

    prices = "".join(
        f"      {shape.billing_owner.value}: "
        f"{_integer(rate.nanos_per_card_hour(shape.billing_owner))},\n"
        for shape in PUBLISHED_SHAPE_RATES
    )
    return (
        "  {\n"
        f"    gpuType: {_string(rate.gpu_type.value)},\n"
        f"    nanosPerCardHour: {{\n{prices}    }},\n"
        "  },\n"
    )


def _most_expensive_first(rates: tuple[PublishedGpuRate, ...]) -> tuple[PublishedGpuRate, ...]:
    """The card's own order is the schedulable list's; the published one is by price.

    The card is held in lockstep with `shared.gpu.SUPPORTED_GPU_TYPES`, so a
    published order that reads by price is produced here rather than by
    reordering either of them. Only one capacity's ranking can lead, and it is
    the fleet's.
    """

    return tuple(
        sorted(rates, key=lambda rate: rate.platform_fleet_nanos_per_card_hour, reverse=True)
    )


def _platform_rates() -> str:
    """What a container moves and keeps, priced for the platform not the capacity.

    Whole nanodollars per whole unit, which is what the card holds and what the
    page renders exactly. The rate each divides down into is the ledger's
    business and is deliberately absent here.
    """

    egress = _integer(PUBLISHED_PLATFORM_RATE.nanos_per_egress_gib)
    volume = _integer(PUBLISHED_PLATFORM_RATE.nanos_per_volume_gib_month)
    return (
        "/**\n"
        " * What a gibibyte of traffic leaving the platform costs.\n"
        " *\n"
        " * A stated zero rather than a figure left off the page: egress is metered, it\n"
        " * reaches the ledger and the invoice, and it reads $0.00 — which is how a\n"
        " * customer can tell the traffic is measured and free rather than unmeasured.\n"
        " */\n"
        f"export const EGRESS_NANOS_PER_GIB = {egress};\n"
        "\n"
        "/**\n"
        " * What this platform charges to run a container on capacity somebody else pays for,\n"
        " * as a percentage of the same container's price on our own fleet.\n"
        " *\n"
        " * Compute only. Volumes and egress are this platform's own infrastructure and are\n"
        " * charged whole wherever the container ran.\n"
        " */\n"
        f"export const CONNECTED_CLOUD_MANAGEMENT_FEE_PERCENT = {_fee_percent()};\n"
        "\n"
        "/** What a gibibyte kept on a volume for a thirty-day month costs. */\n"
        f"export const VOLUME_STORAGE_NANOS_PER_GIB_MONTH = {volume};"
    )


def _fee_percent() -> str:
    """The management fee as the whole percentage the page states it in."""

    percent = CONNECTED_CLOUD_MANAGEMENT_FEE * 100
    if percent != percent.to_integral_value():
        raise ValueError(f"{percent} is not a whole percentage the page can state")
    return str(int(percent))


def _plan_ids() -> str:
    ids = ", ".join(_string(plan.id.value) for plan in PUBLISHED_PLANS)
    return (
        f"export const planIds = [{ids}] as const;\nexport type PlanId = (typeof planIds)[number];"
    )


def _no_card_terms() -> str:
    """What an account gets before anybody can be charged for it.

    Published beside the plans because it is what every account has on its first
    day, and a pricing page that stated only the carded figures would be quoting
    terms nobody starts on.
    """

    return (
        "/* What an account gets before a card is on file. Not a plan — every plan\n"
        "   falls back to these until somebody can be billed. */\n"
        f"export const NO_CARD_INCLUDED_NANOS = {_integer(NO_CARD_INCLUDED_NANOS)};\n"
        f"export const NO_CARD_MAX_CONTAINERS = {_integer(NO_CARD_MAX_CONTAINERS)};"
    )


def _plan_type() -> str:
    return """export type PublishedPlan = {
  name: string;
  summary: string;
  monthlyNanos: number;
  includedNanos: number;
  maxConcurrentContainers: number;
  /** What the plan promises beyond its figures; it never restates one of them. */
  terms: readonly string[];
};"""


def _plans() -> str:
    entries = "".join(_plan(plan) for plan in PUBLISHED_PLANS)
    return (
        "/* Every plan whole: what it is called, what it charges, what it comes with,\n"
        "   and what it promises. A surface offering a plan renders these and keeps no\n"
        "   copy of its own, so a plan added to the card is a plan the pricing page and\n"
        "   the dashboard describe without being edited. */\n"
        "export const publishedPlans = {\n"
        f"{entries}"
        "} as const satisfies Record<PlanId, PublishedPlan>;"
    )


def _plan(plan: PublishedPlan) -> str:
    terms = "".join(f"      {_string(term)},\n" for term in plan.terms)
    return (
        f"  {plan.id.value}: {{\n"
        f"    name: {_string(plan.name)},\n"
        f"    summary: {_string(plan.summary)},\n"
        f"    monthlyNanos: {_integer(plan.monthly_nanos)},\n"
        f"    includedNanos: {_integer(plan.included_nanos)},\n"
        f"    maxConcurrentContainers: {_integer(plan.max_concurrent_containers)},\n"
        f"    terms: [\n{terms}    ],\n"
        "  },\n"
    )


def _string(value: str) -> str:
    """A double-quoted literal, refusing anything that would need escaping.

    Nothing on the card carries a quote or a backslash, and rendering source that
    might is a way to write TypeScript that does not parse.
    """

    if any(character in value for character in '"\\\n'):
        raise ValueError(f"{value!r} cannot be rendered as a TypeScript string literal")
    return f'"{value}"'


def _integer(value: int) -> str:
    return _group(str(value))


def _group(digits: str) -> str:
    """Thousands separated by underscores, as the figures are read aloud."""

    sign, magnitude = ("-", digits[1:]) if digits.startswith("-") else ("", digits)
    grouped = f"{int(magnitude):_d}"
    return f"{sign}{grouped}"


__all__ = ["REGENERATE_COMMAND", "render_pricing_catalog"]
