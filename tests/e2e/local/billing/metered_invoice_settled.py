"""Prove metered usage survives finalization: a settled invoice, to the nanodollar.

`subscription_and_metered_usage` stops at the draft Stripe would build. A draft
is a calculation; an invoice is a decision. Everything that can still go wrong
happens between them — the period closes, the lines are frozen, the allowance is
applied, and Stripe collects or does not — and none of it had ever been observed
against this platform's own numbers.

So this run puts an account on the plan through the public route, runs one real
container priced to more than a cent, waits for the whole chain to close, and
then closes the billing period. Stripe raises the invoice, finalizes it, applies
the allowance and settles it. What is then compared is the metered line's own
quantity against the ledger's summed `cost_nanos` for the same usage records, as
exact integers, on an invoice nobody can edit any more.

Two things are worth stating plainly about what this does and does not prove.

The period is closed by resetting the subscription's billing cycle anchor rather
than by advancing a Stripe test clock. The sandbox credential this repository
holds carries `billing_clock_read` and not `billing_clock_write`, so no test
clock can be created with it; an anchor reset is the same event asked for
directly — Stripe ends the period, raises the invoice for the metered usage
inside it, finalizes it and settles it, all on their side. What it does not
reproduce is a month passing: this invoice's `billing_reason` is
`subscription_update` rather than `subscription_cycle`, and it carries no plan
fee, because `proration_behavior=none` keeps the closure from re-charging or
refunding the subscription itself.

The metered total is settled by the allowance rather than by the card, and that
is the correct outcome rather than a shortfall: the plan includes a hundred
dollars of usage, and no container a single worker can run gets near it. What the
run proves about collection is therefore exact — the allowance covered precisely
the cents the usage came to, and the invoice closed at nothing owed — and what it
leaves unproven is a metered charge that exceeds the allowance and reaches the
card. The money that does move here is the plan itself, on the invoice
subscribing raised.

```sh
uv run python -m tests.e2e.local.billing.metered_invoice_settled --live \
  --confirm-account acct_...
```

Everything the run creates is removed by `tests.e2e.local.billing.cleanup`, which
this module calls on its way out. Paid invoices are left where they are: they are
the record that money moved.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from database.repositories.billing import BillingAccountRepository
from lazycloud.config import reset_settings_cache
from lazycloud.control import control_workspace_scope
from provider_stripe.api import StripeObject, read
from provider_stripe.catalog import SUBSCRIPTION_PRICE_LOOKUP_KEYS
from pydantic import Field
from shared.billing_accounts import BillingAccount
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.http.billing import BillingSummaryResponse
from tests.e2e._support.process import LivePrerequisiteError, blocked
from tests.e2e.local.billing.cleanup import RunResources, run_cleanup
from tests.e2e.local.billing.gate import (
    CARD_SESSION_ROUTE,
    SUBSCRIBE_ROUTE,
    TEST_PAYMENT_METHOD,
    BillingGate,
    RunAccount,
    billing_gate,
    create_run_account,
)
from tests.e2e.local.billing.ledger import (
    COMPUTE_METER,
    PreviewLine,
    PreviewLines,
    drain_metered_usage,
    ledger_cost_nanos,
    read_run_ledger,
)

SOURCE_ROOT = Path(__file__).resolve().parent

CLIENT_TIMEOUT_SECONDS = 1_800.0
TASK_TIMEOUT_SECONDS = 900

SETTLEMENT_DEADLINE_SECONDS = 300.0
SETTLEMENT_POLL_SECONDS = 5.0
SETTLED_INVOICE_STATUSES = frozenset({"paid", "void", "uncollectible"})

SUMMARY_ROUTE = "/api/v1/billing/summary"


class _Price(StripeObject):
    id: str = ""
    lookup_key: str | None = None


class _Item(StripeObject):
    id: str = ""
    price: _Price = Field(default_factory=_Price)
    current_period_start: int = 0
    current_period_end: int = 0


class _Items(StripeObject):
    data: list[_Item] = Field(default_factory=list)


class _Subscription(StripeObject):
    id: str = ""
    status: str = ""
    latest_invoice: str = ""
    items: _Items = Field(default_factory=_Items)


class _PaymentMethod(StripeObject):
    id: str = ""


class _StatusTransitions(StripeObject):
    finalized_at: int | None = None
    paid_at: int | None = None


class _PretaxCredit(StripeObject):
    amount: int = 0
    type: str = ""


class _Invoice(StripeObject):
    id: str = ""
    status: str = ""
    billing_reason: str = ""
    attempted: bool = False
    subtotal: int = 0
    total: int = 0
    amount_due: int = 0
    amount_paid: int = 0
    status_transitions: _StatusTransitions = Field(default_factory=_StatusTransitions)
    total_pretax_credit_amounts: list[_PretaxCredit] = Field(default_factory=list)
    lines: PreviewLines = Field(default_factory=PreviewLines)

    @property
    def credit_applied(self) -> int:
        return sum(entry.amount for entry in self.total_pretax_credit_amounts)

    def metered_lines(self) -> tuple[PreviewLine, ...]:
        return tuple(line for line in self.lines.data if line.metered)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "billing_reason": self.billing_reason,
            "attempted": self.attempted,
            "subtotal_cents": self.subtotal,
            "credit_applied_cents": self.credit_applied,
            "total_cents": self.total,
            "amount_due_cents": self.amount_due,
            "amount_paid_cents": self.amount_paid,
            "finalized_at": self.status_transitions.finalized_at,
            "paid_at": self.status_transitions.paid_at,
            "lines": {
                line.price_name: {"quantity": line.quantity_decimal, "amount_cents": line.amount}
                for line in self.lines.data
            },
        }


class _Monetary(StripeObject):
    value: int = 0


class _TransactionAmount(StripeObject):
    monetary: _Monetary | None = None


class _CreditsApplied(StripeObject):
    invoice: str = ""


class _Debit(StripeObject):
    type: str = ""
    amount: _TransactionAmount = Field(default_factory=_TransactionAmount)
    credits_applied: _CreditsApplied | None = None


class _CreditTransaction(StripeObject):
    id: str = ""
    type: str = ""
    credit_grant: str = ""
    debit: _Debit | None = None

    def applied_to(self, invoice_id: str) -> int:
        if self.debit is None or self.debit.credits_applied is None:
            return 0
        if self.debit.credits_applied.invoice != invoice_id:
            return 0
        return self.debit.amount.monetary.value if self.debit.amount.monetary else 0


class _CreditTransactionList(StripeObject):
    data: list[_CreditTransaction] = Field(default_factory=list)


@dataclass(slots=True)
class _Run:
    account: RunAccount
    provider_customer_id: str = ""
    provider_subscription_id: str = ""
    provider_credit_grant_id: str = ""
    settled_invoice_id: str = ""

    def resources(self) -> RunResources:
        return RunResources(
            user_id=self.account.user_id,
            workspace_id=self.account.workspace_id,
            provider_customer_id=self.provider_customer_id,
            provider_subscription_id=self.provider_subscription_id,
            provider_credit_grant_id=self.provider_credit_grant_id,
        )

    def identifiers(self) -> dict[str, str]:
        return {
            "workspace_id": self.account.workspace_id,
            "workspace_name": self.account.workspace_name,
            "user_id": self.account.user_id,
            "customer_id": self.provider_customer_id,
            "subscription_id": self.provider_subscription_id,
            "credit_grant_id": self.provider_credit_grant_id,
            "settled_invoice_id": self.settled_invoice_id,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-account", default="")
    args = parser.parse_args(argv)
    try:
        gate = billing_gate(live=args.live, confirm_account=args.confirm_account)
    except LivePrerequisiteError as exc:
        return blocked(exc)
    return _scenario(gate)


def _scenario(gate: BillingGate) -> int:
    run = _Run(account=RunAccount(suffix=secrets.token_hex(6)))
    evidence: dict[str, Any] = {}
    primary: BaseException | None = None
    try:
        create_run_account(gate, run.account)
        evidence["subscription"] = _subscribe(gate, run)
        evidence["container"] = _run_billable_container(gate, run)
        drained = drain_metered_usage(
            gate,
            workspace_id=run.account.workspace_id,
            provider_subscription_id=run.provider_subscription_id,
        )
        before = _summary(gate, run)
        invoice = _close_the_period(gate, run)
        evidence["settled_invoice"] = _compare(gate, run, invoice, drained.ledger_cost_nanos)
        evidence["allowance_carried"] = _allowance_carried(gate, run, before)
    except BaseException as exc:
        primary = exc
    cleanup = _cleanup(gate, run)
    gate.close()
    report: dict[str, Any] = {
        "accepted": primary is None,
        "account": gate.account_id,
        "run": run.identifiers(),
        "evidence": evidence,
        "cleanup": cleanup,
    }
    if primary is not None:
        print(json.dumps(report, indent=1, sort_keys=True), file=sys.stderr)
        raise primary
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if not cleanup["remaining"] else 1


def _subscribe(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Put the run's account on the plan, and record what the plan itself cost.

    The customer is registered by the card route, the card is attached at Stripe
    because their hosted page is the only thing that can collect one, and the
    subscription is bought by the platform's own route. The invoice that route
    produces is read back for its `amount_paid`: it is the one place in this run
    where money actually leaves a card, and the figure belongs in the evidence
    beside the metered one that does not.
    """

    channel = run.account.channel(gate)
    channel.post(
        CARD_SESSION_ROUTE,
        {"return_url": gate.public_url, "cancel_url": gate.public_url},
    )
    registered = _billing_account(gate, run)
    if registered is None or not registered.provider_customer_id:
        raise RuntimeError("the card route registered no customer for the run's account")
    run.provider_customer_id = registered.provider_customer_id
    method = read(
        _PaymentMethod,
        gate.client,
        "POST",
        f"/payment_methods/{TEST_PAYMENT_METHOD}/attach",
        data=[("customer", run.provider_customer_id)],
    )
    gate.provider.set_default_payment_method(
        provider_customer_id=run.provider_customer_id,
        provider_payment_method_id=method.id,
    )
    summary = BillingSummaryResponse.model_validate(channel.post(SUBSCRIBE_ROUTE))
    if not summary.subscribed:
        raise RuntimeError("the subscription route answered that the account is not subscribed")
    account = _billing_account(gate, run)
    if account is None or not account.provider_subscription_id:
        raise RuntimeError("subscribing wrote no provider_subscription_id to the account row")
    run.provider_subscription_id = account.provider_subscription_id
    run.provider_credit_grant_id = account.provider_credit_grant_id

    subscription = _subscription(gate, run)
    if subscription.status != "active":
        raise RuntimeError(f"Stripe reports the subscription {subscription.status}, not active")
    carried = sorted(item.price.lookup_key or item.price.id for item in subscription.items.data)
    if carried != sorted(SUBSCRIPTION_PRICE_LOOKUP_KEYS):
        raise RuntimeError(f"the subscription carries {carried}, not the four published prices")
    first = _invoice(gate, subscription.latest_invoice)
    if first.status != "paid" or first.amount_paid <= 0:
        raise RuntimeError(
            f"the plan's own invoice is {first.status} with {first.amount_paid} paid; "
            "the run has no working card"
        )
    return {
        "status": subscription.status,
        "price_lookup_keys": carried,
        "credit_grant": run.provider_credit_grant_id,
        "plan_invoice": first.summary(),
    }


def _billing_account(gate: BillingGate, run: _Run) -> BillingAccount | None:
    with gate.database.session() as session:
        return BillingAccountRepository(session).get_by_user(run.account.user_id)


def _run_billable_container(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Run one real container big enough and long enough to cost whole cents."""

    from tests.e2e.local.billing.workload_billable_container import (
        APP_NAME,
        HOLD_SECONDS,
        REQUESTED_CORES,
        REQUESTED_MEMORY,
        billable_container,
    )

    billable_container.endpoint = gate.endpoint
    billable_container.token = run.account.token
    billable_container.timeout = CLIENT_TIMEOUT_SECONDS
    # The SDK resolves a workspace from the ambient profile unless one is in
    # scope, and the ambient profile is the administrator who created the run.
    reset_settings_cache()
    started_at = datetime.now(UTC)
    with control_workspace_scope(run.account.workspace_name):
        billable_container.deploy(workspace=run.account.workspace_name, source_root=SOURCE_ROOT)
        call = billable_container.spawn(hold_seconds=HOLD_SECONDS)
        observed = call.get(timeout_seconds=TASK_TIMEOUT_SECONDS, poll_interval_seconds=5)
    held = float(observed["held_seconds"])
    if held < HOLD_SECONDS:
        raise RuntimeError(f"the container reported {held}s held, less than the {HOLD_SECONDS}s")
    return {
        "app": APP_NAME,
        "cpu_cores": REQUESTED_CORES,
        "memory": REQUESTED_MEMORY,
        "held_seconds": held,
        "task_id": call.task_id,
        "started_at": started_at.isoformat(),
    }


def _close_the_period(gate: BillingGate, run: _Run) -> _Invoice:
    """End the billing period and let Stripe raise, finalize and settle the invoice.

    `proration_behavior=none` because the subject is the metered usage: with
    prorations the closure would credit the unused part of the plan and charge
    the next period at the same time, and the invoice would be three arguments at
    once. Without them the invoice is exactly the usage of the period that just
    ended.
    """

    closed = read(
        _Subscription,
        gate.client,
        "POST",
        f"/subscriptions/{run.provider_subscription_id}",
        data=[("billing_cycle_anchor", "now"), ("proration_behavior", "none")],
    )
    if not closed.latest_invoice:
        raise RuntimeError("closing the period produced no invoice")
    run.settled_invoice_id = closed.latest_invoice
    deadline = time.monotonic() + SETTLEMENT_DEADLINE_SECONDS
    while True:
        invoice = _invoice(gate, run.settled_invoice_id)
        credits = _credits_applied(gate, run, invoice.id)
        with gate.database.session() as session:
            ledger = read_run_ledger(session, run.account.workspace_id)
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "invoice": invoice.summary(),
            "credit_transactions_against_this_invoice": credits,
            "ledger_segment_cost_nanos": ledger.segment_cost_nanos,
            "billing_meter_outbox_unsettled": [row.payload() for row in ledger.unsettled],
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if invoice.status in SETTLED_INVOICE_STATUSES:
            return invoice
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"invoice {invoice.id} was still {invoice.status} after "
                f"{SETTLEMENT_DEADLINE_SECONDS:.0f}s; last cycle: "
                f"{json.dumps(cycle, sort_keys=True)}"
            )
        time.sleep(SETTLEMENT_POLL_SECONDS)


def _compare(gate: BillingGate, run: _Run, invoice: _Invoice, drained_nanos: int) -> dict[str, Any]:
    """The comparison this scenario exists for, against an invoice nobody can edit."""

    if invoice.status != "paid":
        raise RuntimeError(f"invoice {invoice.id} settled as {invoice.status}, not paid")
    if invoice.status_transitions.finalized_at is None:
        raise RuntimeError(f"invoice {invoice.id} reads paid without ever being finalized")
    with gate.database.session() as session:
        ledger = read_run_ledger(session, run.account.workspace_id)
    owed = ledger_cost_nanos(gate, ledger, COMPUTE_METER)
    if owed != drained_nanos:
        raise RuntimeError(
            f"the ledger held {drained_nanos} nanodollars when the period closed and {owed} "
            "when the invoice was read; usage arrived after the line was frozen"
        )
    billed = _metered_quantity(invoice)
    if billed != owed:
        raise RuntimeError(
            f"the settled invoice bills {billed} nanodollars on {COMPUTE_METER} and the ledger "
            f"holds {owed} for the same usage records"
        )
    if invoice.subtotal <= 0:
        raise RuntimeError(
            f"the settled invoice came to {invoice.subtotal} cents, which is too little usage "
            "for an allowance to visibly discount"
        )
    credit = invoice.credit_applied
    applied = _credits_applied(gate, run, invoice.id)
    if credit != invoice.subtotal or invoice.total != 0 or invoice.amount_due != 0:
        raise RuntimeError(
            f"the allowance covered {credit} of {invoice.subtotal} cents and left "
            f"{invoice.amount_due} owed"
        )
    if sum(entry["amount_cents"] for entry in applied) != credit:
        raise RuntimeError(
            f"the invoice records {credit} cents of allowance and the customer's credit ledger "
            f"records {applied}"
        )
    return {
        "invoice": invoice.summary(),
        "meter": COMPUTE_METER,
        "stripe_metered_nanos": billed,
        "ledger_cost_nanos": owed,
        "equal": True,
        "subtotal_cents": invoice.subtotal,
        "allowance_applied_cents": credit,
        "owed_after_allowance_cents": invoice.amount_due,
        "credit_transactions": applied,
        "ledger_segments": ledger.segment_count,
        "meter_events_sent": len(ledger.outbox),
    }


def _metered_quantity(invoice: _Invoice) -> int:
    """The compute meter's own quantity on the settled invoice, as an integer.

    Read from the line rather than recomputed from the meter, because the line is
    what the customer is charged from and the quantity on it is the number that
    has to equal the ledger.
    """

    for line in invoice.metered_lines():
        if line.price_name != "lazycloud_meter_compute_usd":
            continue
        if line.quantity_decimal is None:
            raise RuntimeError(f"invoice {invoice.id} bills compute without a quantity")
        return int(line.quantity_decimal)
    raise RuntimeError(f"invoice {invoice.id} carries no compute line")


def _credits_applied(gate: BillingGate, run: _Run, invoice_id: str) -> list[dict[str, Any]]:
    """What the customer's own credit ledger says was spent on this invoice."""

    listed = read(
        _CreditTransactionList,
        gate.client,
        "GET",
        "/billing/credit_balance_transactions",
        params=[("customer", run.provider_customer_id), ("limit", "100")],
    )
    return [
        {
            "id": transaction.id,
            "credit_grant": transaction.credit_grant,
            "amount_cents": transaction.applied_to(invoice_id),
        }
        for transaction in listed.data
        if transaction.applied_to(invoice_id)
    ]


def _allowance_carried(
    gate: BillingGate, run: _Run, before: BillingSummaryResponse
) -> dict[str, Any]:
    """Whether the platform moved its own allowance when the provider's period did.

    The closure produces `invoice.paid` and `customer.subscription.updated`, and
    the handler reads the subscription rather than either of them. What it does
    with the new cycle is a period this platform opens and an allowance it buys,
    both readable through the account's own summary — so this is the renewal path
    observed end to end, from a delivery nothing here asked for.
    """

    deadline = time.monotonic() + SETTLEMENT_DEADLINE_SECONDS
    while True:
        summary = _summary(gate, run)
        account = _billing_account(gate, run)
        grant = account.provider_credit_grant_id if account is not None else ""
        moved = summary.allowance.period_started_at > before.allowance.period_started_at
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "period_started_at_before": before.allowance.period_started_at.isoformat(),
            "period_started_at_now": summary.allowance.period_started_at.isoformat(),
            "allowance_nanos": summary.allowance.allowance_nanos,
            "remaining_nanos": summary.allowance.remaining_nanos,
            "credit_grant_before": run.provider_credit_grant_id,
            "credit_grant_now": grant,
            "status": summary.status.value,
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if moved and grant and grant != run.provider_credit_grant_id:
            return {
                "period_started_at_before": before.allowance.period_started_at.isoformat(),
                "period_started_at_after": summary.allowance.period_started_at.isoformat(),
                "credit_grant_before": run.provider_credit_grant_id,
                "credit_grant_after": grant,
                "allowance_nanos": summary.allowance.allowance_nanos,
                "status": summary.status.value,
            }
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "the platform did not carry the account into the new period within "
                f"{SETTLEMENT_DEADLINE_SECONDS:.0f}s; last cycle: "
                f"{json.dumps(cycle, sort_keys=True)}"
            )
        time.sleep(SETTLEMENT_POLL_SECONDS)


def _summary(gate: BillingGate, run: _Run) -> BillingSummaryResponse:
    return BillingSummaryResponse.model_validate(run.account.channel(gate).get(SUMMARY_ROUTE))


def _subscription(gate: BillingGate, run: _Run) -> _Subscription:
    return read(_Subscription, gate.client, "GET", f"/subscriptions/{run.provider_subscription_id}")


def _invoice(gate: BillingGate, invoice_id: str) -> _Invoice:
    return read(
        _Invoice,
        gate.client,
        "GET",
        f"/invoices/{invoice_id}",
        params=[("expand[]", "lines.data.pricing.price_details.price")],
    )


def _cleanup(gate: BillingGate, run: _Run) -> dict[str, Any]:
    try:
        return run_cleanup(gate.client, gate.admin, run.resources())
    except (InvalidInputError, UpstreamUnavailableError, httpx.HTTPError) as exc:
        return {"failed": str(exc), "remaining": ["cleanup did not complete"]}


if __name__ == "__main__":
    raise SystemExit(main())
