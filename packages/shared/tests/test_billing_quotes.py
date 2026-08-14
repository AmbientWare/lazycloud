from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from shared.billing_quotes import (
    BilledDimension,
    LedgerBasis,
    LedgerComponent,
    MeteredSpan,
    PricedSpan,
    Quote,
    QuotedUnit,
    UnpricedReason,
    UnpricedSpan,
    price_span,
)

_START = datetime(2026, 8, 13, 11, 59, 59, 250_000, tzinfo=UTC)
_END = _START + timedelta(seconds=30, microseconds=750)


def _quote(*, effective_at: datetime, valid_until: datetime | None, rate: str) -> Quote:
    return Quote(
        component=LedgerComponent.Cpu,
        unit=QuotedUnit.CoreSeconds,
        rate_nanos_per_unit=Decimal(rate),
        pricing_version=f"v-{rate}",
        effective_at=effective_at,
        valid_until=valid_until,
    )


def _open_quote(rate: str) -> Quote:
    return _quote(effective_at=_START - timedelta(days=1), valid_until=None, rate=rate)


_LAYOUTS: dict[str, tuple[Quote, ...]] = {
    "single-rate": (_open_quote("277.777777777"),),
    "boundary-on-span-start": (
        _quote(
            effective_at=_START - timedelta(days=1),
            valid_until=_START,
            rate="100",
        ),
        _quote(effective_at=_START, valid_until=None, rate="311.5"),
    ),
    "boundary-on-span-end": (
        _quote(effective_at=_START - timedelta(days=1), valid_until=_END, rate="100"),
        _quote(effective_at=_END, valid_until=None, rate="311.5"),
    ),
    "boundary-mid-millisecond": (
        _quote(
            effective_at=_START - timedelta(days=1),
            valid_until=_START + timedelta(seconds=7, microseconds=500),
            rate="100",
        ),
        _quote(
            effective_at=_START + timedelta(seconds=7, microseconds=500),
            valid_until=None,
            rate="0",
        ),
    ),
    "three-rates-back-to-back": (
        _quote(
            effective_at=_START - timedelta(days=1),
            valid_until=_START + timedelta(seconds=10, microseconds=1),
            rate="100",
        ),
        _quote(
            effective_at=_START + timedelta(seconds=10, microseconds=1),
            valid_until=_START + timedelta(seconds=20, microseconds=999),
            rate="0.000000000001",
        ),
        _quote(
            effective_at=_START + timedelta(seconds=20, microseconds=999),
            valid_until=None,
            rate="311.5",
        ),
    ),
}


@pytest.mark.parametrize("layout", sorted(_LAYOUTS), ids=sorted(_LAYOUTS))
def test_split_span_tiles_its_interval_without_gap_overlap_or_lost_quantity(layout: str) -> None:
    """Every part of a quantity is billed once, at the rate in force where it fell."""

    span = MeteredSpan(
        component=LedgerComponent.Cpu,
        basis=LedgerBasis.Reserved,
        started_at=_START,
        ended_at=_END,
        quantity=Decimal("30000.75"),
    )

    pricing = price_span(span, _LAYOUTS[layout])

    assert isinstance(pricing, PricedSpan)
    segments = pricing.segments
    assert segments[0].started_at == span.started_at
    assert segments[-1].ended_at == span.ended_at
    assert [segment.index for segment in segments] == list(range(len(segments)))
    assert all(earlier.ended_at == later.started_at for earlier, later in pairwise(segments))
    assert all(segment.ended_at > segment.started_at for segment in segments)
    assert all(segment.quote.covers(segment.started_at) for segment in segments)
    assert sum(segment.duration_ms for segment in segments) == (
        (span.ended_at - span.started_at) // timedelta(milliseconds=1)
    )
    assert sum((segment.quantity for segment in segments), Decimal(0)) == span.quantity
    assert pricing.cost_nanos == sum(segment.cost_nanos for segment in segments)


def test_an_instant_no_rate_covers_is_unpriced_rather_than_free() -> None:
    span = MeteredSpan(
        component=LedgerComponent.Cpu,
        basis=LedgerBasis.Reserved,
        started_at=_START,
        ended_at=_END,
        quantity=Decimal("30000.75"),
    )

    pricing = price_span(span, (_quote(effective_at=_END, valid_until=None, rate="311.5"),))

    assert pricing == UnpricedSpan(
        dimension=BilledDimension.ComputeRuntime,
        gap_started_at=_START,
        gap_ended_at=_END,
        reason=UnpricedReason.NoPublishedRate,
    )
