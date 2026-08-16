"""Prove metered usage survives finalization: a settled invoice, to the nanodollar.

`subscription_and_metered_usage` stops at the draft Stripe would build. A draft
is a calculation; an invoice is a decision. Everything that can still go wrong
happens between them — the period closes, the lines are frozen, the allowance is
applied, and Stripe collects or does not — and none of it had ever been observed
against this platform's own numbers.

So this run puts an account on the Team plan through the public route, runs one
real container priced to more than a cent, waits for the whole chain to close, and
then closes the billing period. Stripe raises the invoice, finalizes it, applies
the allowance and settles it. What is then compared is the metered line's own
quantity against the ledger's summed `cost_nanos` for the same usage records, as
exact integers, on an invoice nobody can edit any more.

Two things are worth stating plainly about what this does and does not prove.

The period is closed by resetting the subscription's billing cycle anchor rather
than by advancing a Stripe test clock, because a clock has to be bound to a
customer when that customer is created and this run lets the product register its
own. An anchor reset is the same event asked for directly — Stripe ends the
period, raises the invoice for the metered usage inside it, finalizes it and
settles it, all on their side. What it does not reproduce is a month passing:
this invoice's `billing_reason` is `subscription_update` rather than
`subscription_cycle`, and it carries no plan fee, because `proration_behavior=none`
keeps the closure from re-charging or refunding the subscription itself. A month
actually passing is `tests.e2e.local.billing.subscription_renewal_over_time`.

The metered total is settled by the allowance rather than by the card, and that
is the correct outcome rather than a shortfall: the plan includes a hundred
dollars of usage, and no container a single worker can run gets near it. What the
run proves about collection is therefore exact — the allowance covered precisely
the cents the usage came to, and the invoice closed at nothing owed — and what it
leaves unproven is a metered charge that exceeds the allowance and reaches the
card. The money that does move here is the plan change itself, on the proration
invoice Stripe raises and charges the moment the price is swapped.

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
from foundation.environment_file import load_environment_file
from lazycloud.config import reset_settings_cache
from lazycloud.control import control_workspace_scope
from provider_stripe.api import read
from provider_stripe.catalog import subscription_price_lookup_keys
from shared.billing_plans import BillingPlanId
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.http.billing import BillingSummaryResponse
from tests.e2e._support.process import LivePrerequisiteError, blocked
from tests.e2e.local.billing.cleanup import RunResources, run_cleanup
from tests.e2e.local.billing.gate import (
    SUBSCRIBE_ROUTE,
    BillingGate,
    RunAccount,
    attach_default_card,
    billing_account,
    billing_gate,
    create_run_account,
    plan_allowance,
    register_customer,
)
from tests.e2e.local.billing.ledger import (
    COMPUTE_METER,
    Invoice,
    Subscription,
    credits_applied,
    drain_metered_usage,
    invoice,
    ledger_cost_nanos,
    read_run_ledger,
    subscription,
)

SOURCE_ROOT = Path(__file__).resolve().parent

CLIENT_TIMEOUT_SECONDS = 1_800.0
TASK_TIMEOUT_SECONDS = 900

SETTLEMENT_DEADLINE_SECONDS = 300.0
SETTLEMENT_POLL_SECONDS = 5.0
SETTLED_INVOICE_STATUSES = frozenset({"paid", "void", "uncollectible"})

COMPUTE_PRICE_LOOKUP_KEY = "lazycloud_meter_compute_usd"
SUMMARY_ROUTE = "/api/v1/billing/summary"


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
        evidence["container"] = _run_billable_container(gate, run)
        drained = drain_metered_usage(
            gate,
            workspace_id=run.account.workspace_id,
            provider_subscription_id=run.provider_subscription_id,
        )
        before = _summary(gate, run)
        settled = _close_the_period(gate, run)
        evidence["settled_invoice"] = _compare(gate, run, settled, drained.ledger_cost_nanos)
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
    """Move the run's account up a plan, and read what the change itself cost.

    Opening the card page registered the customer and put them on the free plan,
    so subscribing swaps the price on the subscription that already exists. Stripe
    raises and charges the prorated difference at once, and that invoice is read
    back for its `amount_paid`: it is the one place in this run where money
    actually leaves a card, and the figure belongs in the evidence beside the
    metered one that never does.
    """

    channel = run.account.channel(gate)
    free = register_customer(gate, run.account)
    run.provider_customer_id = free.provider_customer_id
    run.provider_subscription_id = free.provider_subscription_id
    attach_default_card(gate, free.provider_customer_id)

    summary = BillingSummaryResponse.model_validate(
        channel.post(SUBSCRIBE_ROUTE, {"plan": BillingPlanId.Team.value})
    )
    if summary.plan is None or summary.plan.id is not BillingPlanId.Team:
        raise RuntimeError(
            f"the subscription route answered with plan {summary.plan.id if summary.plan else None}"
        )
    team = billing_account(gate, run.account)
    if team is None or team.plan is not BillingPlanId.Team:
        raise RuntimeError("subscribing did not record the Team plan on the account row")
    run.provider_subscription_id = team.provider_subscription_id
    run.provider_credit_grant_id = team.provider_credit_grant_id

    live = subscription(gate, run.provider_subscription_id)
    if live.status != "active":
        raise RuntimeError(f"Stripe reports the subscription {live.status}, not active")
    expected = sorted(subscription_price_lookup_keys(BillingPlanId.Team))
    if live.price_names != expected:
        raise RuntimeError(f"the subscription carries {live.price_names}, not {expected}")
    proration = invoice(gate, live.latest_invoice)
    if proration.status != "paid" or proration.amount_paid <= 0:
        raise RuntimeError(
            f"the plan change's proration invoice is {proration.status} with "
            f"{proration.amount_paid} paid; the plan change was not collected"
        )
    return {
        "status": live.status,
        "price_lookup_keys": live.price_names,
        "credit_grant": run.provider_credit_grant_id,
        "allowance_nanos": plan_allowance(summary).allowance_nanos,
        "proration_invoice": proration.summary(),
    }


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


def _close_the_period(gate: BillingGate, run: _Run) -> Invoice:
    """End the billing period and let Stripe raise, finalize and settle the invoice.

    `proration_behavior=none` because the subject is the metered usage: with
    prorations the closure would credit the unused part of the plan and charge
    the next period at the same time, and the invoice would be three arguments at
    once. Without them the invoice is exactly the usage of the period that just
    ended.
    """

    closed = read(
        Subscription,
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
        current = invoice(gate, run.settled_invoice_id)
        applied = credits_applied(
            gate, provider_customer_id=run.provider_customer_id, invoice_id=current.id
        )
        with gate.database.session() as session:
            ledger = read_run_ledger(session, run.account.workspace_id)
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "invoice": current.summary(),
            "credit_transactions_against_this_invoice": applied,
            "ledger_segment_cost_nanos": ledger.segment_cost_nanos,
            "billing_meter_outbox_unsettled": [row.payload() for row in ledger.unsettled],
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if current.status in SETTLED_INVOICE_STATUSES:
            return current
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"invoice {current.id} was still {current.status} after "
                f"{SETTLEMENT_DEADLINE_SECONDS:.0f}s; last cycle: "
                f"{json.dumps(cycle, sort_keys=True)}"
            )
        time.sleep(SETTLEMENT_POLL_SECONDS)


def _compare(gate: BillingGate, run: _Run, settled: Invoice, drained_nanos: int) -> dict[str, Any]:
    """The comparison this scenario exists for, against an invoice nobody can edit."""

    if settled.status != "paid":
        raise RuntimeError(f"invoice {settled.id} settled as {settled.status}, not paid")
    if settled.status_transitions.finalized_at is None:
        raise RuntimeError(f"invoice {settled.id} reads paid without ever being finalized")
    with gate.database.session() as session:
        ledger = read_run_ledger(session, run.account.workspace_id)
    owed = ledger_cost_nanos(gate, ledger, COMPUTE_METER)
    if owed != drained_nanos:
        raise RuntimeError(
            f"the ledger held {drained_nanos} nanodollars when the period closed and {owed} "
            "when the invoice was read; usage arrived after the line was frozen"
        )
    billed = settled.metered_nanos(COMPUTE_PRICE_LOOKUP_KEY)
    if billed != owed:
        raise RuntimeError(
            f"the settled invoice bills {billed} nanodollars on {COMPUTE_METER} and the ledger "
            f"holds {owed} for the same usage records"
        )
    if settled.subtotal <= 0:
        raise RuntimeError(
            f"the settled invoice came to {settled.subtotal} cents, which is too little usage "
            "for an allowance to visibly discount"
        )
    credit = settled.credit_applied
    applied = credits_applied(
        gate, provider_customer_id=run.provider_customer_id, invoice_id=settled.id
    )
    if credit != settled.subtotal or settled.total != 0 or settled.amount_due != 0:
        raise RuntimeError(
            f"the allowance covered {credit} of {settled.subtotal} cents and left "
            f"{settled.amount_due} owed"
        )
    if sum(entry["amount_cents"] for entry in applied) != credit:
        raise RuntimeError(
            f"the invoice records {credit} cents of allowance and the customer's credit ledger "
            f"records {applied}"
        )
    return {
        "invoice": settled.summary(),
        "meter": COMPUTE_METER,
        "stripe_metered_nanos": billed,
        "ledger_cost_nanos": owed,
        "equal": True,
        "subtotal_cents": settled.subtotal,
        "allowance_applied_cents": credit,
        "owed_after_allowance_cents": settled.amount_due,
        "credit_transactions": applied,
        "ledger_segments": ledger.segment_count,
        "meter_events_sent": len(ledger.outbox),
    }


def _allowance_carried(
    gate: BillingGate, run: _Run, before: BillingSummaryResponse
) -> dict[str, Any]:
    """Whether the platform moved its own allowance when the provider's period did.

    The closure produces `invoice.paid` and `customer.subscription.updated`, and
    the handler reads the subscription rather than either of them. What it does
    with the new cycle is a period this platform opens and an allowance it buys,
    both readable through the account's own summary — so this is the renewal path
    observed end to end, from a delivery nothing here asked for.

    The summary reports no allowance at all between the cycle ending and the
    renewal that opens the next one, which is precisely the seam this crosses, so
    an absent one is a cycle to keep polling rather than a failure.
    """

    opened_at = plan_allowance(before).period_started_at
    deadline = time.monotonic() + SETTLEMENT_DEADLINE_SECONDS
    while True:
        summary = _summary(gate, run)
        allowance = summary.plan.allowance if summary.plan is not None else None
        account = billing_account(gate, run.account)
        grant = account.provider_credit_grant_id if account is not None else ""
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "period_started_at_before": opened_at.isoformat(),
            "period_started_at_now": allowance.period_started_at.isoformat() if allowance else "",
            "allowance_nanos": allowance.allowance_nanos if allowance else None,
            "remaining_nanos": allowance.remaining_nanos if allowance else None,
            "credit_grant_before": run.provider_credit_grant_id,
            "credit_grant_now": grant,
            "status": summary.status.value,
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        moved = allowance is not None and allowance.period_started_at > opened_at
        if moved and grant and grant != run.provider_credit_grant_id:
            return {
                "period_started_at_before": opened_at.isoformat(),
                "period_started_at_after": cycle["period_started_at_now"],
                "credit_grant_before": run.provider_credit_grant_id,
                "credit_grant_after": grant,
                "allowance_nanos": cycle["allowance_nanos"],
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


def _cleanup(gate: BillingGate, run: _Run) -> dict[str, Any]:
    try:
        return run_cleanup(gate.client, gate.admin, run.resources())
    except (InvalidInputError, UpstreamUnavailableError, httpx.HTTPError) as exc:
        return {"failed": str(exc), "remaining": ["cleanup did not complete"]}


if __name__ == "__main__":
    raise SystemExit(main())
