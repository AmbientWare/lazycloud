"""What the pricing page quotes and what the platform bills are one rate card.

`apps/web` compiles the card into the marketing pages, which render without
calling the API. That copy is generated from `shared.billing_rate_card` rather
than transcribed from it, so the only way the two can disagree is a generated
file left behind by a change to the card — which is what the comparison below
catches, and why it is a byte comparison rather than a reading of TypeScript.

The stakes are the same in both directions. A rate on the page the platform does
not hold is a quote nobody honours; a rate the platform holds that the page omits
is a charge that arrives unannounced. Egress and volume storage are in scope for
exactly that reason: they are published at a stated zero, and a zero that quietly
became a number would be the worst version of this failure.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from shared.billing_rate_card import PUBLISHED_COMPUTE_RATES
from shared.billing_rate_card_typescript import REGENERATE_COMMAND, render_pricing_catalog

_CATALOG = Path(__file__).resolve().parents[2] / "apps/web/src/routes/-marketing/pricingCatalog.ts"

_STORED_STEP = Decimal("1E-12")
"""The smallest step `NUMERIC(30, 12)` keeps.

A published figure whose per-second rate falls between two of these is rounded
on the way into the rate row, and the platform then bills a rate the page never
stated.
"""


def test_the_page_is_published_from_the_card_the_platform_bills() -> None:
    assert _CATALOG.read_text() == render_pricing_catalog(), (
        f"the published pricing catalog is stale; regenerate it with {REGENERATE_COMMAND}"
    )


def test_every_published_rate_survives_the_column_that_stores_it() -> None:
    """A figure the rate column cannot hold is a price nobody published.

    The per-hour figures on the page divide down to the per-second rates the
    ledger multiplies; one needing more than twelve decimal places would be
    rounded on the way into `NUMERIC(30, 12)`, and the platform would bill a rate
    the page never stated.
    """

    for rate in PUBLISHED_COMPUTE_RATES:
        for figure in (
            rate.nanos_per_container_second,
            rate.nanos_per_cpu_core_second,
            rate.nanos_per_memory_gib_second,
            rate.nanos_per_gpu_card_second,
        ):
            assert figure.quantize(_STORED_STEP) == figure, rate
