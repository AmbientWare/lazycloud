"""What the pricing page quotes and what the platform bills are one rate card.

`apps/web` reads the card through the public pricing contract. The contract is
derived directly from `shared.billing_rate_card`, so the browser holds no second
copy of its figures.

The stakes are the same in both directions. A rate on the page the platform does
not hold is a quote nobody honours; a rate the platform holds that the page omits
is a charge that arrives unannounced. The second is the harder one to see, so the
figures the page has no line for are asserted here rather than left to whoever
next reads the card beside the page.

The card rounds per-second prices down to the database's decimal precision and
rejects a positive price too small to store.
"""

from __future__ import annotations

from decimal import Decimal

from database.tables.billing_rates import ComputeRateTable, PlatformRateTable
from shared.billing_quotes import BYTES_PER_GIB
from shared.billing_rate_card import (
    PUBLISHED_GPU_RATES,
    PUBLISHED_PLATFORM_RATE,
    PUBLISHED_SHAPE_RATES,
    SECONDS_PER_30_DAY_MONTH,
    STORED_RATE_STEP,
)
from shared.usage import UsageBillingOwner
from sqlalchemy import Numeric


def test_the_page_states_every_figure_the_platform_charges() -> None:
    """Nothing on the card is billed without a line on the page saying so.

    The page lists what a container's *resources* cost on the fleet — cores,
    memory, cards — and has no line for the container itself, nor any price for
    hardware somebody else hosts. Both are shapes of the page rather than facts
    about the card, so a card that outgrew either would be billed at a figure no
    reader was shown. What a container costs in a customer's own account is the
    one figure the page does state as a share, so it needs no assertion here.

    Asserted against the card rather than the HTTP mapping, because
    `publish-rates` writes these figures into the rate tables without serving a
    pricing request.
    """

    for shape in PUBLISHED_SHAPE_RATES:
        assert shape.nanos_per_container_hour == 0, (
            f"{shape.billing_owner.value} prices a container before its resources, "
            "which the pricing page has no line for"
        )

    self_hosted = {
        rate.nanos_per_card_hour(UsageBillingOwner.SelfHosted) for rate in PUBLISHED_GPU_RATES
    }
    for shape in PUBLISHED_SHAPE_RATES:
        if shape.billing_owner is UsageBillingOwner.SelfHosted:
            self_hosted |= {shape.nanos_per_cpu_core_hour, shape.nanos_per_memory_gib_hour}
    assert self_hosted == {0}, (
        "hardware somebody else hosts is no longer free, and the pricing page states "
        "no figure for it at all"
    )


def test_platform_rates_never_charge_more_than_the_figure_they_publish() -> None:
    """The direction the platform rates round in, which is the whole of their design.

    Storage's monthly price is converted to byte-seconds. Rounding down keeps
    that conversion from charging more than the customer was quoted.
    """

    charged_per_gib_month = (
        PUBLISHED_PLATFORM_RATE.nanos_per_volume_byte_second
        * BYTES_PER_GIB
        * SECONDS_PER_30_DAY_MONTH
    )
    assert charged_per_gib_month <= PUBLISHED_PLATFORM_RATE.nanos_per_volume_gib_month


def test_the_rounding_step_is_the_precision_the_rate_columns_keep() -> None:
    """The card rounds to a step it names, and the database keeps another.

    `shared` cannot import `database`, so the figure `_stored_rate` quantizes to
    is a hand-copy of the rate columns' scale. Narrow those columns and the
    database re-rounds a rate the card had already settled — in whichever
    direction it chooses, which may be upward, and a rate above the published
    figure is the one thing the rounding design exists to prevent. Widen them and
    every rate is truncated further than it needs to be.
    """

    columns = (
        PlatformRateTable.__table__.c.nanos_per_egress_byte,
        PlatformRateTable.__table__.c.nanos_per_volume_byte_second,
        ComputeRateTable.__table__.c.nanos_per_cpu_core_second,
        ComputeRateTable.__table__.c.nanos_per_gpu_card_second,
    )
    for column in columns:
        stored = column.type
        assert isinstance(stored, Numeric), column.name
        assert stored.scale is not None, column.name
        assert Decimal(1).scaleb(-stored.scale) == STORED_RATE_STEP, column.name
