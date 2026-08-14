"""The published rate card, rendered as the TypeScript the pricing page compiles.

The marketing pages render without calling the API, so the card has to exist in
the browser bundle. Written by hand that is a transcription, and a transcription
of a price is the one kind of drift a customer discovers by being charged
something the published page did not say — so it is generated from the card
instead, and the generated file is checked rather than trusted.

Only what the card owns is rendered: the figures, the owners, the models, the
effective date, and what each plan charges. Plan names and the prose beside them
belong to the page, which joins them on `id`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from shared.billing_rate_card import (
    PUBLISHED_GPU_RATES,
    PUBLISHED_PLANS,
    PUBLISHED_PLATFORM_RATE,
    PUBLISHED_SHAPE_RATES,
    RATES_EFFECTIVE_ON,
    PublishedGpuRate,
)
from shared.payments import BILLING_CURRENCY

REGENERATE_COMMAND = (
    "uv run lazycloud-admin billing write-pricing-catalog "
    "--output apps/web/src/routes/-marketing/pricingCatalog.ts"
)
"""How a reader of the generated file makes it current again.

Named in the file itself and in the failure of the check that compares them,
because a generated artifact whose regeneration has to be looked up is one people
edit by hand instead.
"""

_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
"""Spelled out here rather than through `strftime`, whose month names follow the
locale of whichever process happens to run the generator."""


def render_pricing_catalog() -> str:
    """The whole generated module, ending in the newline a file ends with."""

    blocks = (
        _header(),
        f"export const BILLING_CURRENCY = {_string(BILLING_CURRENCY)};",
        f"export const RATES_EFFECTIVE_ON = {_string(_effective_on(RATES_EFFECTIVE_ON))};",
        _billing_owners(),
        _shape_rate_type(),
        _shape_rates(),
        _gpu_rate_type(),
        _gpu_rates(),
        _platform_rates(),
        _derived_platform_rates(),
        _plan_ids(),
        _plan_type(),
        _plans(),
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

   Figures are nanodollars — billionths of a dollar — per hour of a whole unit: a
   container, a core, a gibibyte, a card. They are exact, so the page renders a
   published rate rather than a rounded one. */"""


def _billing_owners() -> str:
    owners = ", ".join(_string(shape.billing_owner.value) for shape in PUBLISHED_SHAPE_RATES)
    return (
        f"export const billingOwners = [{owners}] as const;\n"
        "export type BillingOwner = (typeof billingOwners)[number];"
    )


def _shape_rate_type() -> str:
    return """export type PublishedShapeRate = {
  /** What a container costs before any of its resources are counted. */
  nanosPerContainerHour: number;
  nanosPerCpuCoreHour: number;
  nanosPerMemoryGibHour: number;
};"""


def _shape_rates() -> str:
    entries = "".join(
        f"  {shape.billing_owner.value}: {{\n"
        f"    nanosPerContainerHour: {_integer(shape.nanos_per_container_hour)},\n"
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
  platformFleetNanosPerCardHour: number;
  connectedCloudNanosPerCardHour: number;
};"""


def _gpu_rates() -> str:
    entries = "".join(
        f"  {{\n"
        f"    gpuType: {_string(rate.gpu_type.value)},\n"
        f"    platformFleetNanosPerCardHour: {_integer(rate.platform_fleet_nanos_per_card_hour)},\n"
        "    connectedCloudNanosPerCardHour: "
        f"{_integer(rate.connected_cloud_nanos_per_card_hour)},\n"
        "  },\n"
        for rate in _by_fleet_price(PUBLISHED_GPU_RATES)
    )
    return (
        "/* Cheapest first on LazyCloud capacity. The two columns rank differently, so\n"
        "   one order has to lead. */\n"
        "export const publishedGpuRates = [\n"
        f"{entries}"
        "] as const satisfies readonly PublishedGpuRate[];"
    )


def _by_fleet_price(rates: tuple[PublishedGpuRate, ...]) -> tuple[PublishedGpuRate, ...]:
    """The card's own order is the schedulable list's; the page's is by price.

    The page is a table somebody reads top to bottom, and the card is held in
    lockstep with `shared.gpu.SUPPORTED_GPU_TYPES`, so the ordering is applied
    here rather than by reordering either of them.
    """

    return tuple(sorted(rates, key=lambda rate: rate.platform_fleet_nanos_per_card_hour))


def _platform_rates() -> str:
    return (
        "/* Published at a stated zero rather than left off the page. Both are metered,\n"
        "   both reach the ledger and the invoice, and both read $0.00 — which is how a\n"
        "   customer can tell the traffic and the storage are measured and free rather\n"
        "   than unmeasured. */\n"
        "export const publishedPlatformRates = {\n"
        f"  nanosPerEgressByte: {_number(PUBLISHED_PLATFORM_RATE.nanos_per_egress_byte)},\n"
        "  nanosPerVolumeByteSecond: "
        f"{_number(PUBLISHED_PLATFORM_RATE.nanos_per_volume_byte_second)},\n"
        "} as const;"
    )


def _derived_platform_rates() -> str:
    egress = _number(PUBLISHED_PLATFORM_RATE.nanos_per_egress_gib)
    volume = _number(PUBLISHED_PLATFORM_RATE.nanos_per_volume_gib_month)
    return (
        "/** What a gibibyte of egress costs, from the per-byte rate exactly. */\n"
        f"export const EGRESS_NANOS_PER_GIB = {egress};\n"
        "\n"
        "/**\n"
        " * What a gibibyte kept for a thirty-day month costs, from the per-byte-second\n"
        " * rate exactly.\n"
        " */\n"
        f"export const VOLUME_STORAGE_NANOS_PER_GIB_MONTH = {volume};"
    )


def _plan_ids() -> str:
    ids = ", ".join(_string(plan.id.value) for plan in PUBLISHED_PLANS)
    return (
        f"export const planIds = [{ids}] as const;\nexport type PlanId = (typeof planIds)[number];"
    )


def _plan_type() -> str:
    return """export type PublishedPlan = {
  monthlyNanos: number;
  includedNanos: number;
};"""


def _plans() -> str:
    entries = "".join(
        f"  {plan.id.value}: {{ monthlyNanos: {_integer(plan.monthly_nanos)}, "
        f"includedNanos: {_integer(plan.included_nanos)} }},\n"
        for plan in PUBLISHED_PLANS
    )
    return (
        "/* What a plan charges and what it comes with. What it is called and how it is\n"
        "   described belongs to the page, which joins the two on the id. */\n"
        "export const publishedPlans = {\n"
        f"{entries}"
        "} as const satisfies Record<PlanId, PublishedPlan>;"
    )


def _effective_on(day: date) -> str:
    return f"{day.day} {_MONTH_NAMES[day.month - 1]} {day.year}"


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


def _number(value: Decimal) -> str:
    """A plain decimal literal, never an exponent.

    `Decimal` renders small figures in scientific notation, which TypeScript
    accepts but nobody comparing a page against a rate card can read.
    """

    whole, _, fraction = format(value, "f").partition(".")
    fraction = fraction.rstrip("0")
    return f"{_group(whole)}.{fraction}" if fraction else _group(whole)


def _group(digits: str) -> str:
    """Thousands separated by underscores, as the figures are read aloud."""

    sign, magnitude = ("-", digits[1:]) if digits.startswith("-") else ("", digits)
    grouped = f"{int(magnitude):_d}"
    return f"{sign}{grouped}"


__all__ = ["REGENERATE_COMMAND", "render_pricing_catalog"]
