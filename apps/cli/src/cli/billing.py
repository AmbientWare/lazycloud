from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_rates import ComputeRateRepository, PlatformRateRepository
from lazycloud.cli.components.cards import result_card
from lazycloud.cli.components.output import emit, table
from provider_stripe import METER_EVENT_BACKFILL_DAYS, PublishedCatalog, StripeSettings
from rich.console import Group
from shared.billing_rate_card import (
    METERED_RATE_VERSION,
    PUBLISHED_COMPUTE_RATES,
    PUBLISHED_PLANS,
    PUBLISHED_PLATFORM_RATE,
)
from shared.errors import ConflictError
from shared.timestamps import utc_now

from cli.components.results import emit_result
from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

billing_app = typer.Typer(
    help=(
        "What the platform bills against: the rate card, where it is published, "
        "and usage nothing has priced."
    )
)


@billing_app.command("publish-rates")
def publish_rates(
    ctx: typer.Context,
    effective_at: Annotated[
        str,
        typer.Option(
            "--effective-at",
            help="The instant these rates start applying, ISO-8601 with an offset (…Z or +00:00).",
        ),
    ],
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm", help="Write the rates. Without it, only report what would change."
        ),
    ] = False,
) -> None:
    """Write the published rate card into the tables pricing reads.

    `--effective-at` has no default. A rate boundary is the one input nobody
    should infer: every millisecond either side of it is billed at a different
    number, and a boundary chosen by whenever somebody happened to run a command
    is one nobody can explain to a customer afterwards.

    The rates themselves are refused if they would reach back over usage the
    ledger has already frozen, so the operator cannot reprice a figure a customer
    has been shown. Running twice with the same instant publishes nothing the
    second time, which is what lets a deployment run this on every sync; figures
    that disagree with what that instant already holds are refused rather than
    written over, and the run says which of the two it found for each rate.

    Without `--confirm` the write is still attempted and then rolled back, so a
    dry run answers whether it would be accepted rather than only what it would
    contain. There is no un-publish: a rate boundary is a figure customers are
    charged either side of, so the run that predicts a refusal is the one worth
    having.
    """

    moment = _instant(effective_at)
    client = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin)
    )
    compute_rates: list[dict[str, str | int]] = []
    payload: dict[str, object] = {
        "pricing_version": METERED_RATE_VERSION,
        "effective_at": moment.isoformat(),
        "compute_rates": compute_rates,
        # Both units. The stored rate is what the ledger multiplies, and the
        # whole-unit figure beside it is what the pricing page states. An operator
        # checking a cutover against the page should not have to
        # multiply by 2,783,138,807,808,000 to find a factor-of-ten error.
        #
        # Plain decimals, never exponents: `Decimal` renders a figure this small
        # as `1.7965E-8`.
        "nanos_per_egress_byte": format(PUBLISHED_PLATFORM_RATE.nanos_per_egress_byte, "f"),
        "nanos_per_egress_gib": PUBLISHED_PLATFORM_RATE.nanos_per_egress_gib,
        "nanos_per_volume_byte_second": format(
            PUBLISHED_PLATFORM_RATE.nanos_per_volume_byte_second, "f"
        ),
        "nanos_per_volume_gib_month": PUBLISHED_PLATFORM_RATE.nanos_per_volume_gib_month,
        "written": confirm,
    }
    try:
        # One transaction: a rate card half written is a shape class priced
        # against a version its neighbours do not share.
        #
        # Attempted either way, and kept only on `--confirm`. Everything that can
        # refuse this does so inside the write. That includes a boundary that
        # reaches back over frozen usage or a figure this instant already holds at
        # another number. A dry run that skipped it would report a plan it could
        # not carry out and exit zero doing so. The operator running this before
        # a cutover is asking exactly that question.
        with client.session() as session:
            compute = ComputeRateRepository(session)
            for rate in PUBLISHED_COMPUTE_RATES:
                publication = compute.publish(
                    billing_owner=rate.billing_owner,
                    gpu_type=rate.gpu_type,
                    pricing_version=METERED_RATE_VERSION,
                    effective_at=moment,
                    nanos_per_container_second=rate.nanos_per_container_second,
                    nanos_per_cpu_core_second=rate.nanos_per_cpu_core_second,
                    nanos_per_memory_gib_second=rate.nanos_per_memory_gib_second,
                    nanos_per_gpu_card_second=rate.nanos_per_gpu_card_second,
                )
                compute_rates.append(
                    {
                        "billing_owner": rate.billing_owner.value,
                        "gpu_type": rate.gpu_type or "-",
                        "nanos_per_container_hour": rate.nanos_per_container_hour,
                        "nanos_per_cpu_core_hour": rate.nanos_per_cpu_core_hour,
                        "nanos_per_memory_gib_hour": rate.nanos_per_memory_gib_hour,
                        "nanos_per_gpu_card_hour": rate.nanos_per_gpu_card_hour,
                        "state": publication.value,
                    }
                )
            platform = PlatformRateRepository(session).publish(
                pricing_version=METERED_RATE_VERSION,
                effective_at=moment,
                nanos_per_egress_byte=PUBLISHED_PLATFORM_RATE.nanos_per_egress_byte,
                nanos_per_volume_byte_second=(PUBLISHED_PLATFORM_RATE.nanos_per_volume_byte_second),
            )
            payload["platform_rate_state"] = platform.value
            if confirm:
                session.commit()
            else:
                session.rollback()
    finally:
        client.dispose()
    emit(
        ctx,
        payload=payload,
        view=Group(
            result_card(
                "Rates published" if confirm else "Rate preview",
                {
                    "effective": moment.isoformat(),
                    "pricing version": METERED_RATE_VERSION,
                    "compute rates": len(compute_rates),
                    "platform rate": str(payload["platform_rate_state"]),
                },
                tone="success" if confirm else "info",
                message="No changes were written." if not confirm else "",
            ),
            table(
                "Compute rates",
                ["owner", "gpu", "container/hour", "cpu/hour", "memory/hour", "gpu/hour", "state"],
                [
                    [
                        rate["billing_owner"],
                        rate["gpu_type"],
                        rate["nanos_per_container_hour"],
                        rate["nanos_per_cpu_core_hour"],
                        rate["nanos_per_memory_gib_hour"],
                        rate["nanos_per_gpu_card_hour"],
                        rate["state"],
                    ]
                    for rate in compute_rates
                ],
            ),
            table(
                "Platform rates",
                ["meter", "nanodollars"],
                [
                    ["egress/GiB", payload["nanos_per_egress_gib"]],
                    ["volume/GiB-month", payload["nanos_per_volume_gib_month"]],
                ],
            ),
        ),
    )


@billing_app.command("publish-catalog")
def publish_catalog(
    ctx: typer.Context,
    confirm_account: Annotated[
        str,
        typer.Option(
            "--confirm-account",
            help="The payment-provider account id this credential must belong to (acct_…).",
        ),
    ],
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm", help="Create what is missing. Without it, only report what would be added."
        ),
    ] = False,
) -> None:
    """Make the payment provider agree with the rate card this repository publishes.

    Idempotent by name: every object is addressed by a name this repository
    chose, so a run against an account that already agrees writes nothing. A plan
    whose amount has changed is republished under the same lookup key, because a
    price cannot be edited at the provider, and the old one is retired.

    Usage rates are not published here at all and need no run to change. Every
    metered price is a fixed conversion of nanodollars to money, and the rate
    card is applied on this side before the usage is reported, so a rate that
    moves reaches the next invoice without an object changing.

    `--confirm-account` has no default and is checked against the account the
    credential in hand belongs to before anything is written. A catalog published
    into the wrong account is objects a live account carries until somebody works
    out whether they matter, and a key alone does not say which account it is.

    Run before the first person signs in, and before shipping a new plan. Signing
    in provisions a subscription on the free plan and fails closed if it cannot,
    and a subscription resolves its prices by lookup key. An account whose catalog
    is unpublished refuses every sign-in it receives, and a plan whose
    price is missing refuses everyone the code puts on it.
    """

    plan_prices = {plan.id: plan.monthly_nanos for plan in PUBLISHED_PLANS}
    settings = StripeSettings()
    catalog = settings.catalog()
    try:
        account_id = catalog.account_id()
        if account_id != confirm_account:
            raise ConflictError(f"this credential belongs to {account_id}, not {confirm_account}")
        published = (
            catalog.publish(plan_prices=plan_prices)
            if confirm
            else catalog.published(plan_prices=plan_prices)
        )
    finally:
        catalog.client.close()
    payload = _catalog_payload(published, written=confirm)
    # Which account is already confirmed above; which *kind* of account is not,
    # and the two are different questions. A sandbox that is its own account has
    # its own identifier and is caught by the confirmation; test data on the
    # account being confirmed is not. The operator publishing a catalog is
    # entitled to know which of those they are looking at.
    payload["live_mode"] = settings.live_mode
    emit_result(
        ctx,
        payload=payload,
        title="Catalog published" if confirm else "Catalog preview",
        fields={
            "account": published.account_id,
            "mode": "live" if settings.live_mode else "test",
            "present": sum(entry.present for entry in published.entries),
            "missing": [entry.name for entry in published.missing],
            "repriced": [entry.name for entry in published.stale],
        },
        tone="success" if confirm else "info",
        message="No changes were written." if not confirm else "",
    )


def _catalog_payload(catalog: PublishedCatalog, *, written: bool) -> dict[str, object]:
    """What the account holds, what is absent from it, and what has moved.

    All three, rather than only the missing one: an operator running this against
    an account they have not seen before needs to know what is already there as
    much as what is not, and a figure that changed is the one thing in the run
    that somebody is charged differently for.
    """

    return {
        "account_id": catalog.account_id,
        "written": written,
        # What the account holds at a figure this repository has moved off.
        # Republished when written, and listed either way, because a price
        # changing is worth reading in the output of the run that changed it.
        "repriced": [
            {
                "kind": entry.kind.value,
                "name": entry.name,
                "was": entry.published_summary,
                "now": entry.summary,
            }
            for entry in catalog.stale
        ],
        "present": [
            {"kind": entry.kind.value, "name": entry.name, "summary": entry.summary}
            for entry in catalog.entries
            if entry.present
        ],
        "missing": [
            {"kind": entry.kind.value, "name": entry.name, "summary": entry.summary}
            for entry in catalog.missing
        ],
    }


@billing_app.command("price-unpriced")
def price_unpriced(
    ctx: typer.Context,
    window_from: Annotated[
        str,
        typer.Option(
            "--from", help="Start of the window usage was RECORDED in, ISO-8601 with an offset."
        ),
    ],
    window_to: Annotated[
        str,
        typer.Option("--to", help="End of that window, exclusive, ISO-8601 with an offset."),
    ],
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Price them. Without it, only report what would be priced."),
    ] = False,
) -> None:
    """Price billable usage in a window that has no ledger segment yet.

    Usage is priced as it is recorded, so in ordinary operation this finds
    nothing. It exists for the case ingest cannot reach: usage metered before any
    rate was published, which nothing will revisit on its own. Bounded to a
    window an operator names, because a sweep that decides its own reach is a
    sweep that reprices a month somebody has already been shown.

    Records that already carry a segment are skipped, never rewritten.
    """

    started_at = _instant(window_from)
    ended_at = _instant(window_to)
    # Pricing usage the provider will refuse to meter is the silent revenue loss
    # this subsystem exists to prevent, arriving one layer later: the ledger would
    # hold a cost that no meter event can carry and no invoice can charge.
    # Observed against the account rather than assumed. See the provider's AGENTS.md.
    oldest = utc_now() - timedelta(days=METER_EVENT_BACKFILL_DAYS)
    if started_at < oldest:
        raise typer.BadParameter(
            f"{window_from!r} is beyond the {METER_EVENT_BACKFILL_DAYS}-day window the payment "
            f"provider accepts meter events for; the earliest chargeable start is "
            f"{oldest.isoformat()}"
        )
    client = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin)
    )
    payload: dict[str, object] = {
        "from": started_at.isoformat(),
        "to": ended_at.isoformat(),
        "written": confirm,
    }
    try:
        with client.session() as session:
            ledger = BillingLedgerRepository(session)
            if confirm:
                priced, skipped = ledger.price_unpriced_between(
                    started_at=started_at, ended_at=ended_at
                )
                session.commit()
            else:
                priced, skipped = 0, 0
        payload["priced"] = priced
        payload["skipped"] = skipped
    finally:
        client.dispose()
    emit_result(
        ctx,
        payload=payload,
        title="Usage priced" if confirm else "Usage pricing preview",
        fields={
            "from": started_at.isoformat(),
            "to": ended_at.isoformat(),
            "priced": priced,
            "skipped": skipped,
        },
        tone="success" if confirm else "info",
        message="No changes were written." if not confirm else "",
    )


def _instant(value: str) -> datetime:
    """Parse a rate boundary, refusing one that does not say which instant it is.

    A naive timestamp here would be read as whatever the reading process assumed,
    and every millisecond either side of the boundary bills at a different rate.
    Better to refuse than to guess an offset on a customer's behalf.
    """

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(f"{value!r} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise typer.BadParameter(f"{value!r} names no offset; use a trailing Z or +00:00")
    return parsed.astimezone(UTC)


__all__ = ["billing_app"]
