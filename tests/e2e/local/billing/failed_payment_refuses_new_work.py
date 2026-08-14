"""Prove a failed payment reaches admission: refused work, from a real delivery.

Everything else about billing is bookkeeping until this holds. A card that stops
working has to become a platform that stops starting containers, and the only
thing joining the two is a delivery from Stripe: nothing here polls a card, and
nothing asks the provider on the way into a container start. So the whole path is
exercised end to end and none of it is simulated — the card is replaced at Stripe
by attaching one of their documented test cards, the platform's own handler makes
it the default because that is what it does with a card-saved delivery, Stripe
attempts a real charge against it and refuses, and the failure comes back as
`invoice.payment_failed` over the public endpoint.

What is then read is not the delivery. `_read_standing` treats every such event as
a cue to go and look at the subscription, so what must move is the account's own
standing — and after it moves, a Function that ran a minute earlier must be
refused with `402`. That refusal is the point of the entire path: the customer
sees it, and nothing before it is visible to anybody.

The charge is provoked by adding a seat to the plan rather than by waiting for
the renewal that would provoke it in production. The sandbox credential this
repository holds cannot write test clocks — `billing_clock_read` without
`billing_clock_write` — so a month cannot be made to pass. A mid-cycle plan change
is the same thing from the platform's side: Stripe raises a subscription invoice,
charges the card on file, is refused, marks the subscription `past_due` and
delivers `invoice.payment_failed`. What it does not reproduce is a renewal
specifically, or Stripe's own retry schedule afterwards.

```sh
uv run python -m tests.e2e.local.billing.failed_payment_refuses_new_work --live \
  --confirm-account acct_...
```

Everything the run creates is removed by `tests.e2e.local.billing.cleanup`, which
this module calls on its way out — including the invoice that was never paid,
which is voided rather than left for Stripe to go on chasing.
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
from database.tables.billing_webhook_events import BillingWebhookEventTable
from lazycloud.abstractions.function import Function, FunctionOperationError
from lazycloud.config import reset_settings_cache
from lazycloud.control import control_workspace_scope
from provider_stripe.api import StripeObject, read
from pydantic import Field
from shared.billing_accounts import BillingAccount, BillingAccountStatus
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.http.billing import BillingSummaryResponse
from shared.http.errors import HttpApiError
from sqlalchemy import select
from tests.e2e._support.process import LivePrerequisiteError, blocked
from tests.e2e.local.billing.cleanup import RunResources, run_cleanup
from tests.e2e.local.billing.gate import (
    CARD_SESSION_ROUTE,
    FAILING_PAYMENT_METHOD,
    SUBSCRIBE_ROUTE,
    TEST_PAYMENT_METHOD,
    BillingGate,
    RunAccount,
    billing_gate,
    create_run_account,
)

SOURCE_ROOT = Path(__file__).resolve().parent

CLIENT_TIMEOUT_SECONDS = 900.0
TASK_TIMEOUT_SECONDS = 300
ADMITTED_HOLD_SECONDS = 3.0

DELIVERY_DEADLINE_SECONDS = 240.0
DELIVERY_POLL_SECONDS = 3.0

PAYMENT_FAILED_EVENT = "invoice.payment_failed"
CARD_SAVED_EVENT = "payment_method.attached"
PLAN_PRICE_LOOKUP_KEY = "lazycloud_plan_team_monthly_usd"


class _Price(StripeObject):
    id: str = ""
    lookup_key: str | None = None


class _Item(StripeObject):
    id: str = ""
    quantity: int = 0
    price: _Price = Field(default_factory=_Price)


class _Items(StripeObject):
    data: list[_Item] = Field(default_factory=list)


class _Subscription(StripeObject):
    id: str = ""
    status: str = ""
    latest_invoice: str = ""
    items: _Items = Field(default_factory=_Items)


class _PaymentMethod(StripeObject):
    id: str = ""


class _InvoiceSettings(StripeObject):
    default_payment_method: str | None = None


class _Customer(StripeObject):
    id: str = ""
    invoice_settings: _InvoiceSettings = Field(default_factory=_InvoiceSettings)


class _Invoice(StripeObject):
    id: str = ""
    status: str = ""
    attempted: bool = False
    attempt_count: int = 0
    amount_due: int = 0
    amount_paid: int = 0


class _EventObject(StripeObject):
    id: str = ""
    customer: str | None = None


class _EventData(StripeObject):
    object: _EventObject = Field(default_factory=_EventObject)


class _Event(StripeObject):
    id: str = ""
    type: str = ""
    pending_webhooks: int = 0
    data: _EventData = Field(default_factory=_EventData)


class _EventList(StripeObject):
    data: list[_Event] = Field(default_factory=list)


@dataclass(slots=True)
class _Run:
    account: RunAccount
    provider_customer_id: str = ""
    provider_subscription_id: str = ""
    provider_credit_grant_id: str = ""
    failing_card: str = ""
    unpaid_invoice_id: str = ""

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
            "failing_card": self.failing_card,
            "unpaid_invoice_id": self.unpaid_invoice_id,
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
        function = _deploy(gate, run)
        evidence["admitted_while_paid_up"] = _admitted(run, function)
        evidence["card_replaced"] = _replace_the_card(gate, run)
        evidence["payment_failed"] = _fail_the_charge(gate, run)
        evidence["refused_while_past_due"] = _refused(run, function)
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
    """Put the run's account on the plan with a card that works.

    The working card matters: `create_subscription` refuses rather than leaving
    an incomplete subscription behind, so a run that starts on the failing card
    would never get as far as the failure it is here to cause. What fails later
    has to be a card that was good and then was not.
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
    return {
        "status": subscription.status,
        "account_status": account.status.value,
        "working_card": method.id,
    }


def _billing_account(gate: BillingGate, run: _Run) -> BillingAccount | None:
    with gate.database.session() as session:
        return BillingAccountRepository(session).get_by_user(run.account.user_id)


def _deploy(gate: BillingGate, run: _Run) -> Function[..., dict[str, float]]:
    """Deploy the run's own Function, so the same call can be made twice.

    Imported here rather than at module scope so the app's unique name is minted
    once per process.
    """

    from tests.e2e.local.billing.workload_metered_container import metered_container

    metered_container.endpoint = gate.endpoint
    metered_container.token = run.account.token
    metered_container.timeout = CLIENT_TIMEOUT_SECONDS
    # The SDK resolves a workspace from the ambient profile unless one is in
    # scope, and the ambient profile is the administrator who created the run.
    reset_settings_cache()
    with control_workspace_scope(run.account.workspace_name):
        metered_container.deploy(workspace=run.account.workspace_name, source_root=SOURCE_ROOT)
    return metered_container


def _admitted(run: _Run, function: Function[..., dict[str, float]]) -> dict[str, Any]:
    """The control: this exact call runs while the account is paid up.

    Without it, the refusal later proves only that something went wrong — a
    broken deployment and a refused account are the same failure from outside.
    """

    with control_workspace_scope(run.account.workspace_name):
        call = function.spawn(hold_seconds=ADMITTED_HOLD_SECONDS)
        observed = call.get(timeout_seconds=TASK_TIMEOUT_SECONDS, poll_interval_seconds=1)
    return {"task_id": call.task_id, "held_seconds": float(observed["held_seconds"])}


def _replace_the_card(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Attach a card that cannot be charged, and let the platform adopt it.

    Nothing here sets a default. Attaching is the half a customer does on the
    provider's page; making it the card the charges go to is what the platform's
    own handler does with the delivery, and this waits for exactly that so the
    charge that follows is aimed by production code.
    """

    method = read(
        _PaymentMethod,
        gate.client,
        "POST",
        f"/payment_methods/{FAILING_PAYMENT_METHOD}/attach",
        data=[("customer", run.provider_customer_id)],
    )
    run.failing_card = method.id
    deadline = time.monotonic() + DELIVERY_DEADLINE_SECONDS
    while True:
        event = _event_for(gate, CARD_SAVED_EVENT, object_id=method.id, run=run)
        claims = _claims(gate, event.id) if event is not None else ()
        default = _default_payment_method(gate, run)
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "attached_card": method.id,
            "event": event.id if event else "",
            "pending_webhooks": event.pending_webhooks if event else None,
            "claimed": list(claims),
            "default_payment_method_at_stripe": default,
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if event is not None and claims and default == method.id:
            return {
                "failing_card": method.id,
                "event": event.id,
                "claimed": list(claims),
                "default_payment_method_at_stripe": default,
            }
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "the platform did not adopt the replacement card within "
                f"{DELIVERY_DEADLINE_SECONDS:.0f}s; last cycle: {json.dumps(cycle, sort_keys=True)}"
            )
        time.sleep(DELIVERY_POLL_SECONDS)


def _fail_the_charge(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Make Stripe charge the card now, and wait for the refusal to land here.

    Adding a seat with `always_invoice` is what turns a plan change into an
    immediate subscription invoice rather than a credit carried to the next one.
    Stripe raises it, attempts the card, is refused, and the account's standing
    is then this platform's to change — from the delivery, not from this call.
    """

    subscription = _subscription(gate, run)
    plan_item = next(
        (
            item
            for item in subscription.items.data
            if item.price.lookup_key == PLAN_PRICE_LOOKUP_KEY
        ),
        None,
    )
    if plan_item is None:
        raise RuntimeError("the subscription carries no plan line to charge")
    charged = read(
        _Subscription,
        gate.client,
        "POST",
        f"/subscriptions/{run.provider_subscription_id}",
        data=[
            ("items[0][id]", plan_item.id),
            ("items[0][quantity]", str(plan_item.quantity + 1)),
            ("proration_behavior", "always_invoice"),
            ("payment_behavior", "allow_incomplete"),
        ],
    )
    run.unpaid_invoice_id = charged.latest_invoice
    deadline = time.monotonic() + DELIVERY_DEADLINE_SECONDS
    while True:
        subscription = _subscription(gate, run)
        invoice = (
            read(_Invoice, gate.client, "GET", f"/invoices/{run.unpaid_invoice_id}")
            if run.unpaid_invoice_id
            else _Invoice()
        )
        event = _event_for(gate, PAYMENT_FAILED_EVENT, object_id=invoice.id, run=run)
        claims = _claims(gate, event.id) if event is not None else ()
        account = _billing_account(gate, run)
        standing = account.status if account is not None else None
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "subscription_status_at_stripe": subscription.status,
            "invoice": {
                "id": invoice.id,
                "status": invoice.status,
                "attempted": invoice.attempted,
                "attempt_count": invoice.attempt_count,
                "amount_due_cents": invoice.amount_due,
                "amount_paid_cents": invoice.amount_paid,
            },
            "event": event.id if event else "",
            "pending_webhooks": event.pending_webhooks if event else None,
            "claimed": list(claims),
            "account_status": standing.value if standing is not None else "",
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if event is not None and claims and standing is BillingAccountStatus.PastDue:
            return {
                "subscription_status_at_stripe": subscription.status,
                "invoice_id": invoice.id,
                "invoice_status": invoice.status,
                "charge_attempts": invoice.attempt_count,
                "amount_due_cents": invoice.amount_due,
                "amount_paid_cents": invoice.amount_paid,
                "event": event.id,
                "event_type": event.type,
                "claimed": list(claims),
                "account_status": standing.value,
            }
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "the failed payment did not reach the account's standing within "
                f"{DELIVERY_DEADLINE_SECONDS:.0f}s; last cycle: {json.dumps(cycle, sort_keys=True)}"
            )
        time.sleep(DELIVERY_POLL_SECONDS)


def _refused(run: _Run, function: Function[..., dict[str, float]]) -> dict[str, Any]:
    """The consequence a customer sees: the same call, now answered with 402."""

    try:
        with control_workspace_scope(run.account.workspace_name):
            call = function.spawn(hold_seconds=ADMITTED_HOLD_SECONDS)
    except FunctionOperationError as exc:
        cause = exc.__cause__
        if not isinstance(cause, HttpApiError):
            raise RuntimeError(f"starting work failed without an HTTP answer: {exc}") from exc
        if cause.status_code != httpx.codes.PAYMENT_REQUIRED:
            raise RuntimeError(
                f"starting work was refused with {cause.status_code}, not 402: {cause}"
            ) from exc
        return {"status_code": cause.status_code, "detail": cause.detail, "code": cause.code}
    raise RuntimeError(
        f"the platform started task {call.task_id} for an account whose payment failed"
    )


def _default_payment_method(gate: BillingGate, run: _Run) -> str:
    customer = read(_Customer, gate.client, "GET", f"/customers/{run.provider_customer_id}")
    return customer.invoice_settings.default_payment_method or ""


def _event_for(gate: BillingGate, event_type: str, *, object_id: str, run: _Run) -> _Event | None:
    """Stripe's own record of a delivery about one of this run's objects."""

    if not object_id:
        return None
    listed = read(
        _EventList,
        gate.client,
        "GET",
        "/events",
        params=[("type", event_type), ("limit", "100")],
    )
    for event in listed.data:
        if event.data.object.id != object_id:
            continue
        if event.data.object.customer not in {None, run.provider_customer_id}:
            continue
        return event
    return None


def _claims(gate: BillingGate, event_id: str) -> tuple[str, ...]:
    with gate.database.session() as session:
        rows = session.scalars(
            select(BillingWebhookEventTable).where(BillingWebhookEventTable.event_id == event_id)
        ).all()
    return tuple(f"{row.event_id} {row.event_type} {row.received_at.isoformat()}" for row in rows)


def _subscription(gate: BillingGate, run: _Run) -> _Subscription:
    return read(_Subscription, gate.client, "GET", f"/subscriptions/{run.provider_subscription_id}")


def _cleanup(gate: BillingGate, run: _Run) -> dict[str, Any]:
    try:
        return run_cleanup(gate.client, gate.admin, run.resources())
    except (InvalidInputError, UpstreamUnavailableError, httpx.HTTPError) as exc:
        return {"failed": str(exc), "remaining": ["cleanup did not complete"]}


if __name__ == "__main__":
    raise SystemExit(main())
