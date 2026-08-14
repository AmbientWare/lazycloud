"""Prove one container's cost reaches a real Stripe invoice as the same integer.

It puts one account on the Team plan through the public route, runs one real
GPU-less container in a workspace made for the run, and then follows that
container's cost the whole way: the placement the control plane recorded, the
ledger segments it priced, the outbox rows the pricer queued, the meter events
the scheduler delivered, and finally the draft invoice Stripe builds from them.
The last step is the one that matters — Stripe's metered total for the draft
invoice against the ledger's summed `cost_nanos` for the same usage records,
compared as exact integers. Anything short of equality means what was sent is not
what was billed.

What this stops at is the draft. That the same figures survive finalization and
collection is `tests.e2e.local.billing.metered_invoice_settled`, which closes a
period and reads the settled invoice.

Prerequisites are checked and named by `tests.e2e.local.billing.gate` before
anything is created.

One step is not the platform's to take. A card is collected on Stripe's hosted
page, which no local automation can drive, so the run attaches Stripe's
documented test payment method to the customer the platform registered and makes
it the default through the same adapter call the card-saved webhook makes.
Everything after that is the product's own path.

```sh
uv run python -m tests.e2e.local.billing.subscription_and_metered_usage --live \
  --confirm-account acct_...
```

Everything the run creates is uniquely named and removed by
`tests.e2e.local.billing.cleanup`, which this module calls on its way out and
which stays independently callable with the identifiers printed here. The
published catalog is never touched: it belongs to the account, not to the run.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from foundation.environment_file import load_environment_file
from lazycloud.config import reset_settings_cache
from lazycloud.control import control_workspace_scope
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
    ZERO_RATED_METERS,
    MeteredUsage,
    drain_metered_usage,
    subscription,
)

SOURCE_ROOT = Path(__file__).resolve().parent

CLIENT_TIMEOUT_SECONDS = 900.0
TASK_TIMEOUT_SECONDS = 300


@dataclass(slots=True)
class _Run:
    """Everything one run made, carried so cleanup can name all of it."""

    account: RunAccount
    provider_customer_id: str = ""
    provider_subscription_id: str = ""
    provider_credit_grant_id: str = ""

    def resources(self) -> RunResources:
        return RunResources(
            user_id=self.account.user_id,
            workspace_id=self.account.workspace_id,
            provider_customer_id=self.provider_customer_id,
            provider_subscription_id=self.provider_subscription_id,
            provider_credit_grant_id=self.provider_credit_grant_id,
        )

    def identifiers(self) -> dict[str, str]:
        """What cleanup needs to name this run's objects, and nothing secret."""

        return {
            "workspace_id": self.account.workspace_id,
            "workspace_name": self.account.workspace_name,
            "user_id": self.account.user_id,
            "customer_id": self.provider_customer_id,
            "subscription_id": self.provider_subscription_id,
            "credit_grant_id": self.provider_credit_grant_id,
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
        evidence["container"] = _run_metered_container(gate, run)
        billed = drain_metered_usage(
            gate,
            workspace_id=run.account.workspace_id,
            provider_subscription_id=run.provider_subscription_id,
        )
        evidence["billed"] = _compare(billed)
        evidence["zero_rated_lines"] = _zero_rated_lines(billed)
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
    """Move the run's account up a plan, and check what that did and did not change.

    The account arrives here already able to be billed: opening the card page
    registered the customer, put them on the free plan and bought the grant that
    funds it. So subscribing is a price swapped on the subscription that already
    exists, and the two halves of that are what this checks — the subscription
    identifier survives, because a second subscription would carry the same
    metered prices and split one customer's usage across two invoices; and the
    grant does not, because the free plan's allowance is expired and replaced by
    the one the larger plan includes.
    """

    channel = run.account.channel(gate)
    free = register_customer(gate, run.account)
    run.provider_customer_id = free.provider_customer_id
    run.provider_subscription_id = free.provider_subscription_id
    run.provider_credit_grant_id = free.provider_credit_grant_id
    card = attach_default_card(gate, free.provider_customer_id)

    summary = BillingSummaryResponse.model_validate(channel.post(SUBSCRIBE_ROUTE))
    if summary.plan is None or summary.plan.id is not BillingPlanId.Team:
        raise RuntimeError(
            f"the subscription route answered with plan {summary.plan.id if summary.plan else None}"
        )
    allowance = plan_allowance(summary)
    team = billing_account(gate, run.account)
    if team is None or team.plan is not BillingPlanId.Team:
        raise RuntimeError("subscribing did not record the Team plan on the account row")
    if team.provider_subscription_id != free.provider_subscription_id:
        raise RuntimeError(
            f"subscribing moved the account from subscription {free.provider_subscription_id} to "
            f"{team.provider_subscription_id}; a plan change swaps a price in place"
        )
    if team.provider_credit_grant_id == free.provider_credit_grant_id:
        raise RuntimeError(
            "subscribing left the free plan's grant in place; the account holds the larger "
            "plan on the smaller allowance"
        )
    run.provider_credit_grant_id = team.provider_credit_grant_id

    live = subscription(gate, run.provider_subscription_id)
    if live.status != "active":
        raise RuntimeError(f"Stripe reports the subscription {live.status}, not active")
    expected = sorted(subscription_price_lookup_keys(BillingPlanId.Team))
    if live.price_names != expected:
        raise RuntimeError(f"the subscription carries {live.price_names}, not {expected}")
    return {
        "status": live.status,
        "price_lookup_keys": live.price_names,
        "card": card,
        "plan_before": free.plan.value if free.plan else None,
        "plan_after": team.plan.value,
        "subscription_survived_the_plan_change": True,
        "credit_grant_before": free.provider_credit_grant_id,
        "credit_grant_after": team.provider_credit_grant_id,
        "allowance_nanos": allowance.allowance_nanos,
        "period_started_at": allowance.period_started_at.isoformat(),
        "period_ended_at": allowance.period_ended_at.isoformat(),
    }


def _run_metered_container(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Run one real GPU-less container as the run's own account.

    Imported here rather than at module scope so the app's unique name is minted
    once per process, and invoked inside the run's workspace scope so the
    placement, the usage and the ledger rows all belong to the workspace this run
    will delete.
    """

    from tests.e2e.local.billing.workload_metered_container import (
        APP_NAME,
        HOLD_SECONDS,
        REQUESTED_CORES,
        REQUESTED_MEMORY,
        metered_container,
    )

    metered_container.endpoint = gate.endpoint
    metered_container.token = run.account.token
    metered_container.timeout = CLIENT_TIMEOUT_SECONDS
    # The SDK resolves a workspace from the ambient profile unless one is in
    # scope, and the ambient profile is the administrator who created the run.
    reset_settings_cache()
    started_at = datetime.now(UTC)
    with control_workspace_scope(run.account.workspace_name):
        metered_container.deploy(workspace=run.account.workspace_name, source_root=SOURCE_ROOT)
        call = metered_container.spawn(hold_seconds=HOLD_SECONDS)
        observed = call.get(timeout_seconds=TASK_TIMEOUT_SECONDS, poll_interval_seconds=1)
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


def _compare(billed: MeteredUsage) -> dict[str, Any]:
    """The comparison the whole scenario exists for, as exact integers."""

    stripe_nanos = billed.stripe_metered_totals.get(COMPUTE_METER, 0)
    if stripe_nanos != billed.ledger_cost_nanos:
        raise RuntimeError(
            f"Stripe billed {stripe_nanos} nanodollars on {COMPUTE_METER} and the ledger holds "
            f"{billed.ledger_cost_nanos} for the same usage records"
        )
    return {
        "draft_invoice": billed.invoice.id,
        "draft_invoice_status": billed.invoice.status,
        "meter": COMPUTE_METER,
        "stripe_metered_nanos": stripe_nanos,
        "ledger_cost_nanos": billed.ledger_cost_nanos,
        "equal": True,
        "meter_events_sent": len(billed.ledger.outbox),
        "ledger_segments": billed.ledger.segment_count,
        "ledger_segment_cost_nanos": billed.ledger.segment_cost_nanos,
    }


def _zero_rated_lines(billed: MeteredUsage) -> dict[str, Any]:
    """Whether a dimension with no meter events still prints its $0.00 line.

    The open question the outbox's zero guard turns on: egress and volume storage
    are published at an explicit zero, so nothing is ever queued for them. If the
    subscription's own metered prices put the lines on the invoice regardless,
    then not sending zero-valued events costs a customer nothing they can see.
    """

    queued = sorted(
        row.identifier for row in billed.ledger.outbox if row.meter_event_name in ZERO_RATED_METERS
    )
    lines: dict[str, Any] = {}
    for line in billed.invoice.lines.data:
        if not line.metered:
            continue
        lines[line.price_name] = {
            "quantity": line.quantity_decimal,
            "amount": line.amount,
            "description": line.description,
        }
    return {
        "meter_events_queued_for_zero_rated_dimensions": queued,
        "metered_lines_on_the_draft_invoice": lines,
    }


def _cleanup(gate: BillingGate, run: _Run) -> dict[str, Any]:
    try:
        return run_cleanup(gate.client, gate.admin, run.resources())
    except (InvalidInputError, UpstreamUnavailableError, httpx.HTTPError) as exc:
        return {"failed": str(exc), "remaining": ["cleanup did not complete"]}


if __name__ == "__main__":
    raise SystemExit(main())
