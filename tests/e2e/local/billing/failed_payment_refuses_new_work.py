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

What is then read is not the delivery. The handler treats every such event as a
cue to go and look at the subscription, so what must move is the account's own
standing — and after it moves, a Function that ran a minute earlier must be
refused with `402`. That refusal is the point of the entire path: the customer
sees it, and nothing before it is visible to anybody.

The charge is provoked by adding a seat to the plan rather than by waiting for
the renewal that would provoke it in production, because a Stripe test clock has
to be bound to a customer when that customer is created and this run lets the
product register its own. A mid-cycle plan change is the same thing from the
platform's side: Stripe raises a subscription invoice, charges the card on file,
is refused, marks the subscription `past_due` and delivers
`invoice.payment_failed`. What it does not reproduce is a renewal specifically,
or Stripe's own retry schedule afterwards.

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
from foundation.environment_file import load_environment_file
from lazycloud.abstractions.function import Function, FunctionOperationError
from lazycloud.config import reset_settings_cache
from lazycloud.control import control_workspace_scope
from provider_stripe.api import read
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.http.billing import BillingSummaryResponse
from shared.http.errors import HttpApiError
from tests.e2e._support.process import LivePrerequisiteError, blocked
from tests.e2e.local.billing.cleanup import RunResources, run_cleanup
from tests.e2e.local.billing.gate import (
    FAILING_PAYMENT_METHOD,
    SUBSCRIBE_ROUTE,
    BillingGate,
    RunAccount,
    attach_card,
    attach_default_card,
    billing_account,
    billing_gate,
    create_run_account,
    register_customer,
)
from tests.e2e.local.billing.ledger import (
    Invoice,
    Subscription,
    claims,
    customer,
    event_for_object,
    invoice,
    subscription,
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
    # This process is the entrypoint that owns itself, which is where a
    # developer `.env` is applied; nothing already exported is overridden.
    load_environment_file()
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
    """Put the run's account on the Team plan with a card that works.

    The working card matters: the plan change is charged immediately and refused
    if it cannot be taken, so a run that started on the failing card would never
    get as far as the failure it is here to cause. What fails later has to be a
    card that was good and then was not.
    """

    channel = run.account.channel(gate)
    free = register_customer(gate, run.account)
    run.provider_customer_id = free.provider_customer_id
    run.provider_subscription_id = free.provider_subscription_id
    working = attach_default_card(gate, free.provider_customer_id)

    summary = BillingSummaryResponse.model_validate(channel.post(SUBSCRIBE_ROUTE))
    if summary.plan is None or summary.plan.id is not BillingPlanId.Team:
        raise RuntimeError(
            f"the subscription route answered with plan {summary.plan.id if summary.plan else None}"
        )
    account = billing_account(gate, run.account)
    if account is None or account.plan is not BillingPlanId.Team:
        raise RuntimeError("subscribing did not record the Team plan on the account row")
    run.provider_subscription_id = account.provider_subscription_id
    run.provider_credit_grant_id = account.provider_credit_grant_id
    live = subscription(gate, run.provider_subscription_id)
    if live.status != "active":
        raise RuntimeError(f"Stripe reports the subscription {live.status}, not active")
    if account.status is not BillingAccountStatus.Active:
        raise RuntimeError(f"the account starts {account.status.value}, not active")
    return {
        "status": live.status,
        "account_status": account.status.value,
        "working_card": working,
    }


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

    run.failing_card = attach_card(gate, run.provider_customer_id, token=FAILING_PAYMENT_METHOD)
    deadline = time.monotonic() + DELIVERY_DEADLINE_SECONDS
    while True:
        event = event_for_object(
            gate,
            CARD_SAVED_EVENT,
            object_id=run.failing_card,
            provider_customer_id=run.provider_customer_id,
        )
        claimed = claims(gate, [event.id] if event is not None else [])
        default = customer(gate, run.provider_customer_id).default_payment_method
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "attached_card": run.failing_card,
            "event": event.id if event else "",
            "pending_webhooks": event.pending_webhooks if event else None,
            "claimed": dict(claimed),
            "default_payment_method_at_stripe": default,
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if event is not None and claimed and default == run.failing_card:
            return {
                "failing_card": run.failing_card,
                "event": event.id,
                "claimed": dict(claimed),
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

    live = subscription(gate, run.provider_subscription_id)
    plan_item = live.item_for(PLAN_PRICE_LOOKUP_KEY)
    if plan_item is None:
        raise RuntimeError("the subscription carries no plan line to charge")
    charged = read(
        Subscription,
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
        current = subscription(gate, run.provider_subscription_id)
        unpaid = invoice(gate, run.unpaid_invoice_id) if run.unpaid_invoice_id else Invoice()
        event = event_for_object(
            gate,
            PAYMENT_FAILED_EVENT,
            object_id=unpaid.id,
            provider_customer_id=run.provider_customer_id,
        )
        claimed = claims(gate, [event.id] if event is not None else [])
        account = billing_account(gate, run.account)
        standing = account.status if account is not None else None
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "subscription_status_at_stripe": current.status,
            "invoice": {
                "id": unpaid.id,
                "status": unpaid.status,
                "attempted": unpaid.attempted,
                "attempt_count": unpaid.attempt_count,
                "amount_due_cents": unpaid.amount_due,
                "amount_paid_cents": unpaid.amount_paid,
            },
            "event": event.id if event else "",
            "pending_webhooks": event.pending_webhooks if event else None,
            "claimed": dict(claimed),
            "account_status": standing.value if standing is not None else "",
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if event is not None and claimed and standing is BillingAccountStatus.PastDue:
            return {
                "subscription_status_at_stripe": current.status,
                "invoice_id": unpaid.id,
                "invoice_status": unpaid.status,
                "charge_attempts": unpaid.attempt_count,
                "amount_due_cents": unpaid.amount_due,
                "amount_paid_cents": unpaid.amount_paid,
                "event": event.id,
                "event_type": event.type,
                "claimed": dict(claimed),
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


def _cleanup(gate: BillingGate, run: _Run) -> dict[str, Any]:
    try:
        return run_cleanup(gate.client, gate.admin, run.resources())
    except (InvalidInputError, UpstreamUnavailableError, httpx.HTTPError) as exc:
        return {"failed": str(exc), "remaining": ["cleanup did not complete"]}


if __name__ == "__main__":
    raise SystemExit(main())
