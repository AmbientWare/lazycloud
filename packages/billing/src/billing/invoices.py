from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_periods import BillingPeriodRepository
from shared.billing_ledger import BillingLedgerEntry
from shared.billing_periods import BillingPeriod, BillingPeriodStatus
from shared.errors import ConflictError
from shared.payments import InvoiceLine, PaymentProvider, ProviderInvoice
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from billing.standing import reconcile_standing

_NANOS_PER_CENT = 10_000_000


def to_cents(nanos: int) -> int:
    """Nanodollars to the unit an invoice is denominated in.

    Rounds half toward positive — so a negative half rounds to the smaller
    magnitude — which only shows on the allowance line and cannot escape
    `_apportion`, where each line is the difference between two running totals.

    Only for a total. Rounding each line and adding them does not give this: two
    half-cent lines round to a cent each and to one cent together, so a bill
    built that way disagrees with itself by the number of lines it has.
    `_apportion` is what converts a set of lines.
    """

    return (nanos + _NANOS_PER_CENT // 2) // _NANOS_PER_CENT


def _apportion(amounts: tuple[int, ...]) -> tuple[int, ...]:
    """Convert nanodollar amounts to cents so they sum to the total's rounding.

    Each line takes the cents its running total has reached, minus what earlier
    lines already took. The residue of every rounding is carried into the next
    line rather than dropped, so the lines add up to what the period says is
    owed — which is the figure `issue` refuses to charge without.
    """

    apportioned: list[int] = []
    running = 0
    taken = 0
    for amount in amounts:
        running += amount
        cents = to_cents(running) - taken
        apportioned.append(cents)
        taken += cents
    return tuple(apportioned)


def invoice_lines(
    *,
    period: BillingPeriod,
    entries: tuple[BillingLedgerEntry, ...],
) -> tuple[InvoiceLine, ...]:
    """What the customer reads, in the order they read it.

    Usage first, by metric and model, then the subscription, then what the plan
    covered. The allowance is a line rather than a subtraction inside the usage
    figures, so an invoice says what ran and what was included instead of only
    their difference — and it is capped at the usage it offsets, because an
    allowance larger than the month's usage does not reduce the subscription.
    """

    totals: dict[tuple[str, str], int] = {}
    for entry in entries:
        key = (entry.metric.value, entry.variant)
        totals[key] = totals.get(key, 0) + entry.cost_nanos
    described = [
        (_describe(metric, variant), nanos)
        for (metric, variant), nanos in sorted(totals.items())
        if nanos > 0
    ]
    if period.subscription_cost_nanos > 0:
        described.append((f"{period.plan.value.title()} plan", period.subscription_cost_nanos))
    # Capped at the usage it offsets: an allowance larger than the month's usage
    # does not reduce the subscription, which is what `charge_for` already says.
    covered = min(period.usage_cost_nanos, period.included_cost_nanos)
    if covered > 0:
        described.append(("Included usage", -covered))
    amounts = _apportion(tuple(nanos for _, nanos in described))
    return tuple(
        InvoiceLine(description=description, amount_cents=cents)
        for (description, _), cents in zip(described, amounts, strict=True)
        if cents != 0
    )


PAYMENT_RETRY_INTERVAL = timedelta(hours=24)
"""How long to leave a refused card alone before trying it again.

Something has to change between attempts for a retry to mean anything — a new
card, or the customer's bank relenting — and neither happens in an hour. Charging
every hour would put a run of declines on the customer's statement, and issuers
answer a stream of them by blocking the merchant rather than the card.

Daily rather than never, because a card that was replaced through the provider's
portal is otherwise never charged: the month is settled as failed and nothing
here would look at it again.
"""

COLLECTABLE = frozenset({BillingPeriodStatus.Invoiced, BillingPeriodStatus.PaymentFailed})
"""The states with an issued invoice and money still outstanding."""


def collectable(period: BillingPeriod, *, now: datetime) -> bool:
    """Whether this period is owed money that is worth asking for now.

    An issued month is collectable until it is paid — not only in the moments
    after it is issued. A charge that failed to complete for any reason other
    than a refusal leaves the month issued and uncollected, and treating that as
    finished is a customer whose usage was free, permanently, with nothing
    reporting it.
    """

    if period.status not in COLLECTABLE:
        return False
    if period.status is BillingPeriodStatus.Invoiced:
        return True
    return (
        period.payment_attempted_at is None
        or now - period.payment_attempted_at >= PAYMENT_RETRY_INTERVAL
    )


def payment_outcome(invoice: ProviderInvoice) -> BillingPeriodStatus | None:
    """What the provider's current view of an invoice says became of the money.

    Read from the invoice rather than from the event that pointed at it, so the
    order deliveries arrive in cannot decide the answer.

    Unpaid and never attempted is not an outcome: every invoice is unpaid the
    moment it is issued, and reading that as failure would put every account into
    arrears at the instant it was billed.
    """

    if invoice.paid:
        return BillingPeriodStatus.Paid
    if invoice.status == "void":
        return BillingPeriodStatus.Voided
    if invoice.attempted:
        return BillingPeriodStatus.PaymentFailed
    return None


def _describe(metric: str, variant: str) -> str:
    """A line a customer can check against what they ran.

    The GPU model travels here rather than in the provider's own pricing,
    which is what lets a new model be a rate in this repository and nothing at
    all on the other side.
    """

    label = metric.replace("_", " ").capitalize()
    return f"{label} ({variant})" if variant else label


@dataclass(frozen=True, slots=True)
class BillingInvoiceService:
    """Turns a settled period into an invoice the customer is charged for."""

    session: Session
    payments: Callable[[], PaymentProvider]
    """Resolved only where an invoice is actually sent.

    A month that owes nothing is settled without one, and a deployment that has
    not been given a payment credential must still be able to settle those — the
    close would otherwise fail for every account because one of them might have
    needed a provider."""

    def reserve(self, *, user_id: str, period_start: date) -> bool:
        """Open the invoice this period will be billed through, and name it.

        Split from issuing so the caller can commit between the two. Issuing is
        several round trips to the provider, and once collection is on the
        provider can report the payment before the last of them returns — a
        period that only learned its invoice id at the end of that would not
        exist to be found when the outcome arrived, and the outcome would be
        acknowledged and dropped.

        Reports whether there is anything left to issue. A period that owes
        nothing is finished here, and one already invoiced has nothing to open.
        """

        periods = BillingPeriodRepository(self.session)
        period = self._settled_period(periods, user_id=user_id, period_start=period_start)
        if period is None:
            return False
        # Called for the check as much as the total: a period whose lines do not
        # add up to what it froze must not reach the provider at all.
        _lines, stated = self._lines_for(period)
        if stated == 0:
            # Nothing owed: an invoice for zero is a document somebody still has
            # to read, and a free account that never spent its allowance would
            # receive one every month. Decided on the total rather than line by
            # line, because a free account that did spend inside its allowance
            # has real usage lines and an allowance line that cancels them —
            # every line non-zero, nothing owed. Decided before a payment
            # relationship is required, because a free account has none and that
            # is not an error.
            periods.mark_nothing_owed(user_id=user_id, period_start=period_start)
            return False
        if period.provider_invoice_id:
            return True
        invoice = self.payments().draft_invoice(
            provider_customer_id=self._customer_for(user_id),
            period_key=f"{user_id}:{period.period_start.isoformat()}",
        )
        periods.record_invoice(
            user_id=user_id,
            period_start=period_start,
            provider_invoice_id=invoice.provider_invoice_id,
        )
        return True

    def issue(self, *, user_id: str, period_start: date) -> BillingPeriod:
        """State what the reserved invoice says, and issue it.

        The lines are replaced rather than added to, so a close retried after a
        crash converges on one invoice saying one thing rather than two.
        """

        periods = BillingPeriodRepository(self.session)
        period = self._settled_period(periods, user_id=user_id, period_start=period_start)
        if period is None:
            settled = periods.get(user_id=user_id, period_start=period_start)
            if settled is None:
                raise ConflictError(f"no billing period to invoice for {user_id} at {period_start}")
            return settled
        if not period.provider_invoice_id:
            raise ConflictError(
                f"billing period for {user_id} at {period_start} has no reserved invoice"
            )
        lines, _ = self._lines_for(period)
        customer_id = self._customer_for(user_id)
        invoice = self.payments().fetch_invoice(provider_invoice_id=period.provider_invoice_id)
        if invoice.status == "draft":
            self.payments().replace_invoice_lines(
                provider_invoice_id=period.provider_invoice_id,
                provider_customer_id=customer_id,
                currency=period.currency,
                lines=lines,
            )
            issued = self.payments().finalize_invoice(
                provider_invoice_id=period.provider_invoice_id
            )
        else:
            # Already issued by a run that died before recording it. Finalizing
            # again is impossible and issuing a second is the thing to avoid, so
            # this adopts it rather than starting over.
            issued = invoice
        return periods.mark_invoiced(
            user_id=user_id,
            period_start=period_start,
            provider_invoice_id=issued.provider_invoice_id,
        )

    def collect(
        self, *, user_id: str, period_start: date, now: datetime | None = None
    ) -> BillingPeriod:
        """Charge the card on file, and record what came back.

        Attempted here rather than left to the provider, which does not collect
        on its own for the way these invoices are issued — an invoice nobody
        charges sits open forever and the money never moves.

        The result is written from the response rather than waited for. The
        webhook records the same outcome when it arrives, and agrees: both read
        the invoice, and settling twice on one answer changes nothing.

        A refusal is not a failure of this method. The bank declining is an
        outcome the period records, and raising here would abandon a month that
        is correctly invoiced and correctly unpaid.
        """

        periods = BillingPeriodRepository(self.session)
        period = periods.get(user_id=user_id, period_start=period_start)
        if period is None:
            raise ConflictError(f"no billing period to collect for {user_id} at {period_start}")
        if not collectable(period, now=now or utc_now()):
            return period
        settled = self.payments().pay_invoice(provider_invoice_id=period.provider_invoice_id)
        outcome = payment_outcome(settled)
        if outcome is None:
            return period
        collected = periods.settle(user_id=user_id, period_start=period_start, status=outcome)
        # Immediately, not when a notification catches up. A declined card leaves
        # the account behind from this moment, and whatever reads standing to
        # decide what may run has to see that now rather than in an hour.
        reconcile_standing(self.session, user_id=user_id)
        return collected

    def _settled_period(
        self, periods: BillingPeriodRepository, *, user_id: str, period_start: date
    ) -> BillingPeriod | None:
        """The period to act on, or nothing where there is no work left.

        Refuses an open one: what an invoice states has to be the frozen figure.
        """

        period = periods.get(user_id=user_id, period_start=period_start)
        if period is None:
            raise ConflictError(f"no billing period to invoice for {user_id} at {period_start}")
        if period.status is BillingPeriodStatus.Open:
            raise ConflictError(
                f"billing period for {user_id} at {period_start} is still open; "
                "what it owes is not settled"
            )
        if period.status is BillingPeriodStatus.Closed:
            return period
        return None

    def _lines_for(self, period: BillingPeriod) -> tuple[tuple[InvoiceLine, ...], int]:
        """What this period's invoice says, refused unless it totals what is owed.

        Checked before anything is sent, because finalizing cannot be undone: an
        invoice issued and then found wrong is a real document the customer
        holds, and it can only be voided, never deleted. A disagreement is a
        defect here — the platform decides the amount — so it must not become a
        charge.
        """

        entries = BillingLedgerRepository(self.session).for_period(
            user_id=period.user_id, period_start=period.period_start, period_end=period.period_end
        )
        lines = invoice_lines(period=period, entries=entries)
        expected = to_cents(period.charged_cost_nanos)
        stated = sum(line.amount_cents for line in lines)
        if stated != expected:
            raise ConflictError(
                f"the lines for {period.user_id} at {period.period_start} total {stated} cents "
                f"but the period owes {expected}"
            )
        return lines, stated

    def _customer_for(self, user_id: str) -> str:
        account = BillingAccountRepository(self.session).get_by_user(user_id)
        if account is None or not account.provider_customer_id:
            raise ConflictError(f"account {user_id} has no payment customer and cannot be invoiced")
        return account.provider_customer_id


__all__ = ["BillingInvoiceService", "invoice_lines", "to_cents"]
