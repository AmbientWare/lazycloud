"""Prove a month passing: the renewal invoice, the rolled allowance, and the meter after it.

Every other billing scenario here happens inside one billing period. This one is
about the seam between two, which is the only place several of this platform's
decisions are ever taken: the cycle Stripe rolls, the allowance period this
platform opens to match it, the grant it buys for the new cycle, and the invoice
Stripe raises for the cycle that just ended. Until a month actually passes, none
of that runs — and an allowance that never resets is a customer paying for a
hundred dollars of included usage once.

A month is made to pass with a Stripe test clock. Everything about the
subscription then happens at the clock's time rather than at this host's, which
has one consequence the run has to work around continuously: Stripe refuses a
meter event timestamped more than five minutes after the clock, and this
platform stamps usage with the real time it happened. So the clock is created a
month in the past, the subscription is bought there, and the clock is then
advanced back up to real time before any usage is metered — after which the
period boundary is under an hour away in real time rather than a month.

Two steps cross that boundary rather than one, and the split is the point.
Stripe raises the renewal invoice as the period ends and finalizes it about an
hour later; in production that hour is real time, and this platform's own roll —
delivery, new period, new grant — happens in the seconds after the boundary,
inside it. Advancing straight past finalization would compress an hour into a
second and settle the invoice before any delivery could arrive, which is not
what production does. So the clock stops just past the boundary, the roll is
observed, and only then is it advanced far enough for Stripe to finalize.

One step is not the platform's to take. Stripe binds a customer to a test clock
when the customer is created and never afterwards, so the run creates that
customer itself — with the same email and the same `workspace_id` metadata
`StripeBilling.create_customer` writes — and records it through the repository
production records it through. The platform's own card route then adopts it,
which is what `billing_account_for` does with an account that already names a
customer, and everything after that is production's path: the free subscription
that route provisions, its subscribe route, its pricer, its outbox, its webhook
handler.

```sh
uv run python -m tests.e2e.local.billing.subscription_renewal_over_time \
  --live --confirm-account acct_...
```

Everything the run creates is removed by `tests.e2e.local.billing.cleanup`, which
this module calls on its way out, followed by the test clock. Deleting a clock
deletes the customer, subscription and invoices bound to it, which is why the
clock goes last: cleanup reads and reports them first.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from database.repositories.billing import BillingAccountRepository
from database.repositories.identity import UserRepository
from database.tables.billing_allowance import BillingAllowancePeriodTable
from foundation.environment_file import load_environment_file
from lazycloud.abstractions.function import Function
from lazycloud.config import reset_settings_cache
from lazycloud.control import control_workspace_scope
from provider_stripe import CREDIT_GRANT_SETTLEMENT_GRACE
from provider_stripe.api import StripeObject, read
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.http.billing import BillingSummaryResponse
from shared.timestamps import to_utc, utc_now
from sqlalchemy import select
from tests.e2e._support.process import LivePrerequisiteError, blocked
from tests.e2e.local.billing.cleanup import RunResources, run_cleanup
from tests.e2e.local.billing.gate import (
    CARD_SESSION_ROUTE,
    SUBSCRIBE_ROUTE,
    BillingGate,
    RunAccount,
    attach_default_card,
    billing_account,
    billing_gate,
    create_run_account,
    plan_allowance,
)
from tests.e2e.local.billing.ledger import (
    COMPUTE_METER,
    Event,
    EventList,
    Grant,
    Invoice,
    RunLedger,
    claims,
    credits_applied,
    customer_grants,
    customer_invoices,
    drain_metered_usage,
    invoice,
    ledger_cost_nanos,
    moment,
    outstanding,
    preview_invoice,
    read_run_ledger,
)

SOURCE_ROOT = Path(__file__).resolve().parent

CLIENT_TIMEOUT_SECONDS = 1_800.0
TASK_TIMEOUT_SECONDS = 900

FIRST_CYCLE_MINUTES = 45
"""How far into real time the first period's end is placed.

The subscription is bought a month behind, so its cycle ends this long after the
clock is created. Everything the first cycle has to contain happens inside it:
the image builds, a seven-minute container, the sweep that queues its meter
events, and Stripe's own aggregation. The builds are in that list because
admission refuses an account holding no subscription, so the account has to be
provisioned — which opens the cycle — before anything can be deployed at all.
Longer is a run that idles; shorter is a container still running when its period
closes.
"""

FIRST_CYCLE_MARGIN_SECONDS = 900.0
"""Real time the first cycle must still have left once the workloads are built.

Enough for the container and the drain behind it. Checked once, immediately after
the builds, because that is where a slow one shows up — and refusing there costs
nothing, where discovering it at the boundary wastes the whole run.
"""

BOUNDARY_TOLERANCE_SECONDS = 600.0
"""How far the cycle Stripe actually opens may sit from where the run aimed it.

A calendar month is not a fixed number of days, so the frozen time this run
computes lands the boundary near a target rather than on it. Well outside this
means the arithmetic missed — a clamped day at the end of a month — and the run
refuses immediately rather than after half an hour of work aimed at the wrong
instant.
"""

CLOCK_LEAD_SECONDS = 600
"""How far past real time the clock is parked before usage is metered.

Stripe accepts a meter event timestamped no more than five minutes after the
clock, and a clock is frozen rather than ticking: parked at real time it falls
behind by exactly as long as the container runs. Parked this far ahead it still
covers usage that happened a few minutes after it was set.
"""

FINALIZATION_LEAD_SECONDS = 7_200
"""Clock time past the boundary before Stripe finalizes the invoice it raised.

Their auto-advance is about an hour after the draft is created, measured on this
customer's clock.
"""

BOUNDARY_STEP_SECONDS = 60
"""Clock time past the boundary for the first of the two advances.

Far enough that the period has certainly rolled, near enough that the draft is
nowhere close to finalizing.
"""

CLOCK_DEADLINE_SECONDS = 600.0
CLOCK_POLL_SECONDS = 5.0

RENEWAL_DEADLINE_SECONDS = 600.0
RENEWAL_POLL_SECONDS = 5.0

SECOND_CYCLE_DEADLINE_SECONDS = 1_800.0
SECOND_CYCLE_POLL_SECONDS = 10.0

SETTLED_INVOICE_STATUSES = frozenset({"paid", "void", "uncollectible"})
RENEWAL_BILLING_REASON = "subscription_cycle"
SUBSCRIPTION_ROLLED_EVENT = "customer.subscription.updated"
COMPUTE_PRICE_LOOKUP_KEY = "lazycloud_meter_compute_usd"


class _Clock(StripeObject):
    id: str = ""
    status: str = ""
    frozen_time: int = 0
    deleted: bool = False

    @property
    def frozen_at(self) -> datetime:
        return datetime.fromtimestamp(self.frozen_time, tz=UTC)


class _Customer(StripeObject):
    id: str = ""


@dataclass(frozen=True, slots=True)
class _Cycle:
    """One billing period, as the provider reports it."""

    started_at: datetime
    ended_at: datetime

    def payload(self) -> dict[str, str]:
        return {"start": self.started_at.isoformat(), "end": self.ended_at.isoformat()}


@dataclass(frozen=True, slots=True)
class _AllowancePeriod:
    """One row of what this platform decided an account may spend, and when."""

    started_at: datetime
    ended_at: datetime
    allowance_nanos: int
    spent_nanos: int

    def payload(self) -> dict[str, Any]:
        return {
            "start": self.started_at.isoformat(),
            "end": self.ended_at.isoformat(),
            "allowance_nanos": self.allowance_nanos,
            "spent_nanos": self.spent_nanos,
        }

    def covers(self, cycle: _Cycle) -> bool:
        return self.started_at == cycle.started_at and self.ended_at == cycle.ended_at


@dataclass(slots=True)
class _Run:
    """Everything one run made, carried so cleanup can name all of it."""

    account: RunAccount
    clock_id: str = ""
    boundary_aimed_at: datetime | None = None
    provider_customer_id: str = ""
    provider_subscription_id: str = ""
    first_grant_id: str = ""
    second_grant_id: str = ""
    first_cycle: _Cycle | None = None
    second_cycle: _Cycle | None = None
    renewal_invoice_id: str = ""
    first_cycle_nanos: int = 0
    second_cycle_nanos: int = 0
    metered_before_the_boundary: tuple[str, ...] = field(default_factory=tuple)

    def resources(self) -> RunResources:
        return RunResources(
            user_id=self.account.user_id,
            workspace_id=self.account.workspace_id,
            provider_customer_id=self.provider_customer_id,
            provider_subscription_id=self.provider_subscription_id,
            provider_credit_grant_id=self.second_grant_id or self.first_grant_id,
        )

    def identifiers(self) -> dict[str, str]:
        return {
            "workspace_id": self.account.workspace_id,
            "workspace_name": self.account.workspace_name,
            "user_id": self.account.user_id,
            "test_clock_id": self.clock_id,
            "customer_id": self.provider_customer_id,
            "subscription_id": self.provider_subscription_id,
            "first_credit_grant_id": self.first_grant_id,
            "second_credit_grant_id": self.second_grant_id,
            "renewal_invoice_id": self.renewal_invoice_id,
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
        evidence["registered_on_a_test_clock"] = _register_on_a_test_clock(gate, run)
        first, second = _deploy_the_workloads(gate, run)
        evidence["subscription"] = _subscribe(gate, run)
        evidence["first_cycle_usage"] = _first_cycle_usage(gate, run, first)
        evidence["renewal_raised_and_allowance_rolled"] = _cross_the_boundary(gate, run)
        evidence["renewal_settled"] = _settle_the_renewal(gate, run)
        evidence["grant_at_the_boundary"] = _grant_at_the_boundary(gate, run)
        evidence["second_cycle_usage"] = _second_cycle_usage(gate, run, second)
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


def _deploy_the_workloads(
    gate: BillingGate, run: _Run
) -> tuple[Function[..., dict[str, float]], Function[..., dict[str, float]]]:
    """Build both Functions, as early as an account is allowed to build anything.

    Deployment builds an image, which is the slowest thing this run does and the
    one thing that has nothing to do with billing — so it wants to be first. It
    cannot be: admission refuses an account holding no subscription, and the
    account only holds one once the card route has provisioned it, which is what
    opens the cycle everything after this has to fit inside. So the builds are
    inside the first period by necessity, and what is left of that period is
    checked here rather than discovered at the boundary.

    Imported here rather than at module scope so each app's unique name is minted
    once per process.
    """

    from tests.e2e.local.billing.workload_billable_container import billable_container
    from tests.e2e.local.billing.workload_metered_container import metered_container

    for function in (billable_container, metered_container):
        function.endpoint = gate.endpoint
        function.token = run.account.token
        function.timeout = CLIENT_TIMEOUT_SECONDS
    # The SDK resolves a workspace from the ambient profile unless one is in
    # scope, and the ambient profile is the administrator who created the run.
    reset_settings_cache()
    with control_workspace_scope(run.account.workspace_name):
        billable_container.deploy(workspace=run.account.workspace_name, source_root=SOURCE_ROOT)
        metered_container.deploy(workspace=run.account.workspace_name, source_root=SOURCE_ROOT)
    aimed_at = _require_boundary(run)
    left = (aimed_at - utc_now()).total_seconds()
    print(
        json.dumps(
            {
                "at": utc_now().isoformat(timespec="seconds"),
                "built": "both workloads",
                "boundary_aimed_at": aimed_at.isoformat(),
                "seconds_of_the_first_cycle_left": round(left),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    if left < FIRST_CYCLE_MARGIN_SECONDS:
        raise RuntimeError(
            f"the builds left {left:.0f}s of the first cycle, under the "
            f"{FIRST_CYCLE_MARGIN_SECONDS:.0f}s its container and drain need; raise "
            "FIRST_CYCLE_MINUTES for this host"
        )
    return billable_container, metered_container


def _register_on_a_test_clock(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Put this run's account on a customer bound to a clock a month behind.

    The customer is the one call production makes that this run has to make
    itself: Stripe binds a clock at creation and never afterwards, so a customer
    the platform registered could never have a month pass. It is created with
    exactly what `StripeBilling.create_customer` sends — the account's email and
    its workspace in metadata — and recorded through the repository the card
    route writes, naming nothing else: an account that holds a customer and no
    subscription is a registration that got half way, which is the state the card
    route exists to finish.

    The card route is then called, and that is the point of calling it:
    `billing_account_for` adopts a customer an account already names rather than
    registering a second, and provisions the rest around it — the free
    subscription and the grant that funds it, on a customer whose clock this run
    controls.
    """

    boundary = utc_now() + timedelta(minutes=FIRST_CYCLE_MINUTES)
    run.boundary_aimed_at = boundary
    frozen = _a_month_before(boundary)
    clock = read(
        _Clock,
        gate.client,
        "POST",
        "/test_helpers/test_clocks",
        data=[
            ("frozen_time", _epoch(frozen)),
            ("name", f"e2e-billing-renewal-{run.account.suffix}"),
        ],
    )
    run.clock_id = clock.id
    with gate.database.session() as session:
        user = UserRepository(session).get(run.account.user_id)
    if user is None:
        raise RuntimeError(f"the run's own account is not readable: {run.account.user_id}")
    customer = read(
        _Customer,
        gate.client,
        "POST",
        "/customers",
        data=[
            ("email", user.email),
            ("metadata[workspace_id]", run.account.workspace_id),
            ("test_clock", clock.id),
        ],
    )
    run.provider_customer_id = customer.id
    with gate.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=run.account.user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=customer.id,
            provider_subscription_id="",
            provider_credit_grant_id="",
            plan=None,
        )
        session.commit()

    run.account.channel(gate).post(
        CARD_SESSION_ROUTE,
        {"return_url": gate.public_url, "cancel_url": gate.public_url},
    )
    adopted = billing_account(gate, run.account)
    if adopted is None or adopted.provider_customer_id != customer.id:
        raise RuntimeError(
            "the card route did not adopt the run's customer; the account names "
            f"{adopted.provider_customer_id if adopted else 'nothing'}"
        )
    if not adopted.provider_subscription_id or adopted.plan is not BillingPlanId.Free:
        raise RuntimeError(
            "the card route did not provision the run's account onto the free plan; it holds "
            f"subscription {adopted.provider_subscription_id or 'nothing'} on plan {adopted.plan}"
        )
    card = attach_default_card(gate, customer.id)
    return {
        "test_clock": clock.id,
        "frozen_at": frozen.isoformat(),
        "customer": customer.id,
        "adopted_by_the_card_route": True,
        "provisioned_plan": adopted.plan.value,
        "card": card,
        "boundary_aimed_at": boundary.isoformat(),
    }


def _subscribe(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Move up a plan through the platform's own route, a month behind.

    The cycle Stripe opens is read back and checked against where this run aimed
    the boundary before anything else is done, because every later step is timed
    against it: a period that opened days from where it was meant to would leave
    the run waiting for a boundary it cannot reach.
    """

    channel = run.account.channel(gate)
    summary = BillingSummaryResponse.model_validate(channel.post(SUBSCRIBE_ROUTE))
    if summary.plan is None or summary.plan.id is not BillingPlanId.Team:
        raise RuntimeError(
            f"the subscription route answered with plan {summary.plan.id if summary.plan else None}"
        )
    account = billing_account(gate, run.account)
    if account is None or not account.provider_credit_grant_id:
        raise RuntimeError("subscribing left the account row without the grant it bought")
    run.provider_subscription_id = account.provider_subscription_id
    run.first_grant_id = account.provider_credit_grant_id

    subscription = gate.provider.subscription(provider_subscription_id=run.provider_subscription_id)
    if subscription.status != "active":
        raise RuntimeError(f"Stripe reports the subscription {subscription.status}, not active")
    cycle = _Cycle(
        started_at=subscription.current_period_started_at,
        ended_at=subscription.current_period_ended_at,
    )
    run.first_cycle = cycle
    aimed_at = _require_boundary(run)
    drift = abs((cycle.ended_at - aimed_at).total_seconds())
    if drift > BOUNDARY_TOLERANCE_SECONDS:
        raise RuntimeError(
            f"the first cycle ends {cycle.ended_at.isoformat()}, {drift:.0f}s from the "
            f"{aimed_at.isoformat()} this run aimed at; a calendar month from the frozen instant "
            "is not where it was computed"
        )
    period = _allowance_period_covering(gate, run, cycle)
    if period is None:
        raise RuntimeError(f"subscribing opened no allowance period over {cycle.payload()}")
    grant = read(Grant, gate.client, "GET", f"/billing/credit_grants/{run.first_grant_id}")
    return {
        "status": subscription.status,
        "cycle_1": cycle.payload(),
        "seconds_until_the_boundary": round((cycle.ended_at - utc_now()).total_seconds()),
        "allowance_period": period.payload(),
        "credit_grant": grant.payload(),
        "allowance_nanos": plan_allowance(summary).allowance_nanos,
    }


def _first_cycle_usage(
    gate: BillingGate, run: _Run, function: Function[..., dict[str, float]]
) -> dict[str, Any]:
    """Run a container worth whole cents, and follow its cost to Stripe's own count.

    The clock is carried up to real time first, because the subscription was
    bought a month behind and a meter event stamped a month after the clock is
    refused. It is parked slightly ahead so the container's last window is still
    inside the window Stripe accepts when the sweep gets to it.
    """

    cycle = _require_cycle(run.first_cycle, "first")
    _advance_to(gate, run, _clock_lead(not_past=cycle.ended_at), label="up to real time")
    from tests.e2e.local.billing.workload_billable_container import (
        APP_NAME,
        HOLD_SECONDS,
        REQUESTED_CORES,
        REQUESTED_MEMORY,
    )

    started_at = utc_now()
    with control_workspace_scope(run.account.workspace_name):
        call = function.spawn(hold_seconds=HOLD_SECONDS)
        observed = call.get(timeout_seconds=TASK_TIMEOUT_SECONDS, poll_interval_seconds=5)
    held = float(observed["held_seconds"])
    if held < HOLD_SECONDS:
        raise RuntimeError(f"the container reported {held}s held, less than the {HOLD_SECONDS}s")
    _advance_to(gate, run, _clock_lead(not_past=cycle.ended_at), label="over the usage just run")
    billed = drain_metered_usage(
        gate,
        workspace_id=run.account.workspace_id,
        provider_subscription_id=run.provider_subscription_id,
    )
    stripe_nanos = billed.stripe_metered_totals.get(COMPUTE_METER, 0)
    if stripe_nanos != billed.ledger_cost_nanos:
        raise RuntimeError(
            f"Stripe counted {stripe_nanos} nanodollars on {COMPUTE_METER} and the ledger holds "
            f"{billed.ledger_cost_nanos} for the same usage records"
        )
    run.first_cycle_nanos = billed.ledger_cost_nanos
    run.metered_before_the_boundary = tuple(row.identifier for row in billed.ledger.outbox)
    return {
        "app": APP_NAME,
        "cpu_cores": REQUESTED_CORES,
        "memory": REQUESTED_MEMORY,
        "held_seconds": held,
        "task_id": call.task_id,
        "started_at": started_at.isoformat(),
        "meter": COMPUTE_METER,
        "stripe_metered_nanos": stripe_nanos,
        "ledger_cost_nanos": billed.ledger_cost_nanos,
        "equal": True,
        "meter_events_sent": len(billed.ledger.outbox),
        "seconds_until_the_boundary": round((cycle.ended_at - utc_now()).total_seconds()),
    }


def _cross_the_boundary(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """End the period and watch the platform carry the account into the next one.

    The clock stops a minute past the boundary rather than past finalization.
    What has to be observable is the order production runs in: Stripe rolls the
    cycle and raises a draft, the delivery reaches this platform, and the
    platform opens its own period and buys the grant — all before the invoice
    Stripe will finalize an hour later.

    Six readings a cycle, printed whether or not any moved: what Stripe says the
    subscription's period is now, every invoice on the customer, the deliveries
    Stripe has recorded and whether this platform claimed them, the allowance
    rows the platform holds, the grant its account row names, and both grants at
    the provider.
    """

    first = _require_cycle(run.first_cycle, "first")
    _advance_to(
        gate,
        run,
        first.ended_at + timedelta(seconds=BOUNDARY_STEP_SECONDS),
        label="a minute past the boundary",
    )
    deadline = time.monotonic() + RENEWAL_DEADLINE_SECONDS
    while True:
        subscription = gate.provider.subscription(
            provider_subscription_id=run.provider_subscription_id
        )
        current = _Cycle(
            started_at=subscription.current_period_started_at,
            ended_at=subscription.current_period_ended_at,
        )
        invoices = customer_invoices(gate, run.provider_customer_id)
        renewal = _renewal_invoice(invoices, first)
        deliveries = _deliveries(gate, run)
        periods = _allowance_periods(gate, run)
        account = billing_account(gate, run.account)
        grant_now = account.provider_credit_grant_id if account is not None else ""
        rolled = current.started_at > first.started_at
        opened = next((period for period in periods if period.covers(current)), None)
        claimed = [entry for entry in deliveries if entry["type"] == SUBSCRIPTION_ROLLED_EVENT]
        cycle_report: dict[str, Any] = {
            "at": utc_now().isoformat(timespec="seconds"),
            "cycle_1": first.payload(),
            "subscription_period_now": current.payload(),
            "subscription_status": subscription.status,
            "invoices": [entry.summary() for entry in invoices],
            "renewal_invoice": renewal.id if renewal is not None else "",
            "deliveries": deliveries,
            "allowance_periods": [period.payload() for period in periods],
            "credit_grant_on_the_account_row": grant_now,
            "credit_grant_when_subscribed": run.first_grant_id,
            "grants_at_the_provider": [
                grant.payload() for grant in customer_grants(gate, run.provider_customer_id)
            ],
        }
        print(json.dumps(cycle_report, sort_keys=True), flush=True)
        if (
            rolled
            and renewal is not None
            and opened is not None
            and grant_now
            and grant_now != run.first_grant_id
            and any(entry["claimed"] for entry in claimed)
        ):
            run.second_cycle = current
            run.second_grant_id = grant_now
            run.renewal_invoice_id = renewal.id
            return {
                "cycle_1": first.payload(),
                "cycle_2": current.payload(),
                "renewal_invoice": renewal.summary(),
                "subscription_rolled_delivery": claimed,
                "allowance_period_opened_for_cycle_2": opened.payload(),
                "credit_grant_before": run.first_grant_id,
                "credit_grant_after": grant_now,
                "allowance_periods": [period.payload() for period in periods],
            }
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "the platform did not carry the account into the new period within "
                f"{RENEWAL_DEADLINE_SECONDS:.0f}s; last cycle: "
                f"{json.dumps(cycle_report, sort_keys=True)}"
            )
        time.sleep(RENEWAL_POLL_SECONDS)


def _settle_the_renewal(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Let Stripe finalize and collect the invoice the boundary raised.

    Only now is the clock taken past their auto-advance, so what finalizes does
    so against an account this platform has already rolled — which is the state
    production would be in an hour after a renewal, and the state that decides
    which allowance the invoice can reach.
    """

    first = _require_cycle(run.first_cycle, "first")
    _advance_to(
        gate,
        run,
        first.ended_at + timedelta(seconds=FINALIZATION_LEAD_SECONDS),
        label="past Stripe's finalization",
    )
    deadline = time.monotonic() + RENEWAL_DEADLINE_SECONDS
    while True:
        renewal = invoice(gate, run.renewal_invoice_id)
        applied = credits_applied(
            gate, provider_customer_id=run.provider_customer_id, invoice_id=renewal.id
        )
        deliveries = _deliveries(gate, run)
        cycle_report: dict[str, Any] = {
            "at": utc_now().isoformat(timespec="seconds"),
            "renewal_invoice": renewal.summary(),
            "credit_transactions_against_it": applied,
            "deliveries": deliveries,
        }
        print(json.dumps(cycle_report, sort_keys=True), flush=True)
        if renewal.status in SETTLED_INVOICE_STATUSES:
            return _renewal_evidence(run, renewal, applied, deliveries)
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"the renewal invoice was still {renewal.status} after "
                f"{RENEWAL_DEADLINE_SECONDS:.0f}s; last cycle: "
                f"{json.dumps(cycle_report, sort_keys=True)}"
            )
        time.sleep(RENEWAL_POLL_SECONDS)


def _renewal_evidence(
    run: _Run,
    renewal: Invoice,
    applied: list[dict[str, Any]],
    deliveries: list[dict[str, Any]],
) -> dict[str, Any]:
    """What the settled renewal says, checked against the ledger behind it."""

    if renewal.status != "paid":
        raise RuntimeError(f"the renewal invoice settled as {renewal.status}, not paid")
    if renewal.status_transitions.finalized_at is None:
        raise RuntimeError(f"invoice {renewal.id} reads paid without ever being finalized")
    if renewal.billing_reason != RENEWAL_BILLING_REASON:
        raise RuntimeError(
            f"the invoice at the boundary reads {renewal.billing_reason}, not "
            f"{RENEWAL_BILLING_REASON}; this was not a renewal"
        )
    billed = renewal.metered_nanos(COMPUTE_PRICE_LOOKUP_KEY)
    if billed != run.first_cycle_nanos:
        raise RuntimeError(
            f"the renewal bills {billed} nanodollars of compute and the ledger holds "
            f"{run.first_cycle_nanos} for the first cycle's usage records"
        )
    return {
        "invoice": renewal.summary(),
        "meter": COMPUTE_METER,
        "stripe_metered_nanos": billed,
        "ledger_cost_nanos": run.first_cycle_nanos,
        "equal": True,
        "credit_transactions_against_it": applied,
        "deliveries": deliveries,
    }


def _grant_at_the_boundary(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Prove the settlement grace put the right cycle's allowance on the invoice.

    Credit is applied when an invoice is finalized rather than when it is raised,
    and the invoice for a cycle finalizes about an hour after that cycle ended. A
    grant that expired on the boundary would already be gone when the invoice it
    funds asks for it, and the next cycle's would pay the last cycle's arrears —
    a customer's new month silently funding their old one.

    `CREDIT_GRANT_SETTLEMENT_GRACE` moves both ends of every grant later by the
    same amount, which is what makes the expiring grant outlive its own invoice
    and the incoming one not yet reachable by it. Both halves are checked here,
    against the credit ledger that says which grant actually paid.
    """

    renewal = invoice(gate, run.renewal_invoice_id)
    applied = credits_applied(
        gate, provider_customer_id=run.provider_customer_id, invoice_id=renewal.id
    )
    first = read(Grant, gate.client, "GET", f"/billing/credit_grants/{run.first_grant_id}")
    second = read(Grant, gate.client, "GET", f"/billing/credit_grants/{run.second_grant_id}")
    first_cycle = _require_cycle(run.first_cycle, "first")
    second_cycle = _require_cycle(run.second_cycle, "second")

    _require_grace(first, first_cycle, which="the expiring")
    _require_grace(second, second_cycle, which="the incoming")
    finalized_at = renewal.status_transitions.finalized_at
    if finalized_at is None or first.expires_at is None or finalized_at >= first.expires_at:
        raise RuntimeError(
            f"the renewal finalized at {moment(finalized_at)} and cycle 1's allowance expired at "
            f"{moment(first.expires_at)}; the grant did not outlive the invoice it funds"
        )
    spent_from = {entry["credit_grant"] for entry in applied if entry["amount_cents"]}
    if run.second_grant_id in spent_from:
        raise RuntimeError(
            f"the renewal for cycle 1 was paid from cycle 2's allowance {run.second_grant_id}; "
            "the incoming grant was spendable before the outgoing invoice finalized"
        )
    return {
        "expiring_grant": first.payload(),
        "new_grant": second.payload(),
        "settlement_grace_seconds": int(CREDIT_GRANT_SETTLEMENT_GRACE.total_seconds()),
        "renewal_finalized_at": moment(finalized_at),
        "renewal_subtotal_cents": renewal.subtotal,
        "renewal_credit_applied_cents": renewal.credit_applied,
        "renewal_amount_due_cents": renewal.amount_due,
        "renewal_amount_paid_cents": renewal.amount_paid,
        "credit_transactions_against_the_renewal": applied,
        "expiring_grant_paid_for_it": run.first_grant_id in spent_from,
        "new_grant_paid_for_it": False,
    }


def _require_grace(grant: Grant, cycle: _Cycle, *, which: str) -> None:
    """Both ends of one grant, against the cycle it was bought for.

    The expiry is exact arithmetic on the cycle. The start is the later of the
    shifted cycle start and the instant the grant was bought, so only the shifted
    start is checkable — and only where it is still ahead, which is every grant
    bought for a cycle that has not yet begun.
    """

    expected_expiry = int((cycle.ended_at + CREDIT_GRANT_SETTLEMENT_GRACE).timestamp())
    if grant.expires_at != expected_expiry:
        raise RuntimeError(
            f"{which} allowance expires {moment(grant.expires_at)}, not {moment(expected_expiry)}, "
            "which is its cycle's end plus the settlement grace"
        )
    shifted_start = int((cycle.started_at + CREDIT_GRANT_SETTLEMENT_GRACE).timestamp())
    if grant.effective_at is None or grant.effective_at > shifted_start:
        raise RuntimeError(
            f"{which} allowance becomes spendable {moment(grant.effective_at)}, later than "
            f"{moment(shifted_start)}, which is its cycle's start plus the settlement grace"
        )


def _second_cycle_usage(
    gate: BillingGate, run: _Run, function: Function[..., dict[str, float]]
) -> dict[str, Any]:
    """Run a container in the new period and follow it to Stripe, again.

    The roll moved the period at Stripe and here; whether the meter came with it
    is a separate question, and the one this proves. Usage is stamped with the
    real instant it happened, so the run holds until real time is inside the new
    cycle before starting anything — the point of placing the boundary half an
    hour out rather than a month.

    The comparison is restricted to the meter events queued after the boundary,
    because the ledger keeps the first cycle's rows and the invoice this reads is
    the new period's alone. Equality across the whole workspace would compare a
    month of usage against a period that never held it.
    """

    second = _require_cycle(run.second_cycle, "second")
    _hold_until(gate, run, second)
    from tests.e2e.local.billing.workload_metered_container import (
        APP_NAME,
        HOLD_SECONDS,
        REQUESTED_CORES,
        REQUESTED_MEMORY,
    )

    started_at = utc_now()
    with control_workspace_scope(run.account.workspace_name):
        call = function.spawn(hold_seconds=HOLD_SECONDS)
        observed = call.get(timeout_seconds=TASK_TIMEOUT_SECONDS, poll_interval_seconds=1)
    held = float(observed["held_seconds"])
    if held < HOLD_SECONDS:
        raise RuntimeError(f"the container reported {held}s held, less than the {HOLD_SECONDS}s")
    if started_at < second.started_at:
        raise RuntimeError(
            f"the second container started {started_at.isoformat()}, before the new cycle opened "
            f"at {second.started_at.isoformat()}"
        )
    _advance_to(gate, run, _clock_lead(not_past=second.ended_at), label="over the new usage")
    drained = _drain_the_new_period(gate, run)
    period = _allowance_period_covering(gate, run, second)
    return {
        "app": APP_NAME,
        "cpu_cores": REQUESTED_CORES,
        "memory": REQUESTED_MEMORY,
        "held_seconds": held,
        "task_id": call.task_id,
        "started_at": started_at.isoformat(),
        "cycle_2": second.payload(),
        "allowance_period": period.payload() if period is not None else {},
        **drained,
    }


def _drain_the_new_period(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Poll the chain from the new container to the new period's own draft invoice.

    Every link every cycle, printed whether or not it moved, and the loop ends
    only on all of them agreeing: the rows the pricer queued since the boundary,
    what the ledger holds for exactly those records, and what Stripe has counted
    against the invoice the new period would produce.
    """

    deadline = time.monotonic() + SECOND_CYCLE_DEADLINE_SECONDS
    previous: tuple[Any, ...] | None = None
    still = 0
    while True:
        with gate.database.session() as session:
            whole = read_run_ledger(session, run.account.workspace_id)
        fresh = tuple(
            row for row in whole.outbox if row.identifier not in run.metered_before_the_boundary
        )
        since = RunLedger(
            shapes=whole.shapes,
            segment_count=whole.segment_count,
            segment_cost_nanos=whole.segment_cost_nanos,
            outbox=fresh,
        )
        draft = preview_invoice(gate, run.provider_subscription_id)
        totals = gate.provider.invoice_metered_totals(provider_invoice_id=draft.id)
        owed = ledger_cost_nanos(gate, since, COMPUTE_METER)
        counted = totals.get(COMPUTE_METER, 0)
        cycle_report: dict[str, Any] = {
            "at": utc_now().isoformat(timespec="seconds"),
            "meter_events_queued_since_the_boundary": [row.payload() for row in fresh],
            "ledger_cost_nanos_for_those_records": owed,
            "stripe_metered_totals_on_the_new_period": dict(totals),
            "first_cycle_ledger_cost_nanos": run.first_cycle_nanos,
            "draft_invoice": draft.id,
        }
        signature = (fresh, tuple(sorted(totals.items())), owed)
        still = still + 1 if signature == previous else 0
        previous = signature
        if still >= 2:
            cycle_report["stalled"] = {
                "cycles": still,
                "outstanding": outstanding(since, totals, owed),
            }
        print(json.dumps(cycle_report, sort_keys=True), flush=True)
        if fresh and not since.unsettled and owed > 0 and counted == owed:
            run.second_cycle_nanos = owed
            return {
                "meter": COMPUTE_METER,
                "meter_events_queued_since_the_boundary": [row.payload() for row in fresh],
                "stripe_metered_nanos_on_the_new_period": counted,
                "ledger_cost_nanos_since_the_boundary": owed,
                "equal": True,
                "first_cycle_ledger_cost_nanos": run.first_cycle_nanos,
                "draft_invoice": draft.id,
            }
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "usage in the second cycle did not reach the new period within "
                f"{SECOND_CYCLE_DEADLINE_SECONDS:.0f}s; last cycle: "
                f"{json.dumps(cycle_report, sort_keys=True)}"
            )
        time.sleep(SECOND_CYCLE_POLL_SECONDS)


def _hold_until(gate: BillingGate, run: _Run, cycle: _Cycle) -> None:
    """Hold until real time is inside the new cycle, reading everything meanwhile.

    Not a wait on a state: the instant is arithmetic, known before the loop
    starts, and the loop prints how far off it is beside the signals that could
    make holding pointless — a subscription that stopped being active, or a
    renewal that came undone.
    """

    while True:
        now = utc_now()
        remaining = (cycle.started_at - now).total_seconds()
        subscription = gate.provider.subscription(
            provider_subscription_id=run.provider_subscription_id
        )
        print(
            json.dumps(
                {
                    "at": now.isoformat(timespec="seconds"),
                    "cycle_2_opens_at": cycle.started_at.isoformat(),
                    "seconds_until_real_time_is_inside_it": round(remaining),
                    "subscription_status": subscription.status,
                    "subscription_period_now": _Cycle(
                        started_at=subscription.current_period_started_at,
                        ended_at=subscription.current_period_ended_at,
                    ).payload(),
                    "allowance_periods": [
                        period.payload() for period in _allowance_periods(gate, run)
                    ],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if remaining <= 0:
            return
        if subscription.status != "active":
            raise RuntimeError(
                f"the subscription is {subscription.status} while the run holds for the new cycle"
            )
        time.sleep(min(SECOND_CYCLE_POLL_SECONDS, max(remaining, 1.0)))


def _advance_to(gate: BillingGate, run: _Run, target: datetime, *, label: str) -> None:
    """Move the clock forward and poll until Stripe says it has finished.

    Advancing is asynchronous and lands on every billing boundary it passes, so
    the clock's own reported time is read each cycle rather than assumed from
    the request — it is the only signal that says which boundaries have already
    been processed.
    """

    # Compared in whole seconds throughout, because that is the only precision a
    # clock has: asking for a time with a fraction on it and then waiting for the
    # clock to reach that exact value is a wait nothing can ever satisfy.
    wanted = int(target.timestamp())
    current = _clock(gate, run)
    if wanted <= current.frozen_time:
        return
    read(
        _Clock,
        gate.client,
        "POST",
        f"/test_helpers/test_clocks/{run.clock_id}/advance",
        data=[("frozen_time", str(wanted))],
    )
    deadline = time.monotonic() + CLOCK_DEADLINE_SECONDS
    while True:
        clock = _clock(gate, run)
        print(
            json.dumps(
                {
                    "at": utc_now().isoformat(timespec="seconds"),
                    "advancing": label,
                    "clock_status": clock.status,
                    "clock_now": clock.frozen_at.isoformat(),
                    "clock_target": moment(wanted),
                    "real_now": utc_now().isoformat(timespec="seconds"),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if clock.status == "ready" and clock.frozen_time >= wanted:
            return
        if clock.status == "internal_failure":
            raise RuntimeError(f"Stripe could not advance the clock {label}: {clock.status}")
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"the clock was still {clock.status} at {clock.frozen_at.isoformat()} after "
                f"{CLOCK_DEADLINE_SECONDS:.0f}s advancing {label}"
            )
        time.sleep(CLOCK_POLL_SECONDS)


def _clock_lead(*, not_past: datetime) -> datetime:
    """Where to park the clock: ahead of real time, but inside the current cycle."""

    return min(utc_now() + timedelta(seconds=CLOCK_LEAD_SECONDS), not_past - timedelta(seconds=60))


def _clock(gate: BillingGate, run: _Run) -> _Clock:
    return read(_Clock, gate.client, "GET", f"/test_helpers/test_clocks/{run.clock_id}")


def _renewal_invoice(invoices: Sequence[Invoice], first: _Cycle) -> Invoice | None:
    """The invoice Stripe raised for the cycle that just ended, named by its period."""

    for entry in invoices:
        if entry.billing_reason != RENEWAL_BILLING_REASON:
            continue
        if datetime.fromtimestamp(entry.period_end, tz=UTC) != first.ended_at:
            continue
        return entry
    return None


def _deliveries(gate: BillingGate, run: _Run) -> list[dict[str, Any]]:
    """Stripe's record of what it sent about this subscription, beside this platform's claims.

    Both ends, because they fail differently: Stripe reports a delivery it
    believes it made, and the claim row is one this platform verified, acted on
    and recorded in the same transaction.
    """

    listed = read(
        EventList,
        gate.client,
        "GET",
        "/events",
        params=[
            ("limit", "40"),
            ("types[]", SUBSCRIPTION_ROLLED_EVENT),
            ("types[]", "invoice.paid"),
        ],
    )
    mine: list[Event] = [
        event
        for event in listed.data
        if event.data.object.id in {run.provider_subscription_id, run.renewal_invoice_id}
        or event.data.object.customer == run.provider_customer_id
    ]
    claimed = claims(gate, [event.id for event in mine])
    return [
        {
            "event": event.id,
            "type": event.type,
            "created": moment(event.created),
            "pending_webhooks": event.pending_webhooks,
            "claimed": claimed.get(event.id, ""),
        }
        for event in mine
    ]


def _allowance_periods(gate: BillingGate, run: _Run) -> tuple[_AllowancePeriod, ...]:
    with gate.database.session() as session:
        rows = session.execute(
            select(
                BillingAllowancePeriodTable.period_started_at,
                BillingAllowancePeriodTable.period_ended_at,
                BillingAllowancePeriodTable.allowance_nanos,
                BillingAllowancePeriodTable.spent_nanos,
            )
            .where(BillingAllowancePeriodTable.user_id == run.account.user_id)
            .order_by(BillingAllowancePeriodTable.period_started_at.asc())
        ).all()
    return tuple(
        _AllowancePeriod(
            started_at=to_utc(started_at),
            ended_at=to_utc(ended_at),
            allowance_nanos=int(allowance_nanos),
            spent_nanos=int(spent_nanos),
        )
        for started_at, ended_at, allowance_nanos, spent_nanos in rows
    )


def _allowance_period_covering(
    gate: BillingGate, run: _Run, cycle: _Cycle
) -> _AllowancePeriod | None:
    return next(
        (period for period in _allowance_periods(gate, run) if period.covers(cycle)),
        None,
    )


def _require_boundary(run: _Run) -> datetime:
    if run.boundary_aimed_at is None:
        raise RuntimeError("the run placed no boundary; the test clock was never created")
    return run.boundary_aimed_at


def _require_cycle(cycle: _Cycle | None, which: str) -> _Cycle:
    if cycle is None:
        raise RuntimeError(f"the {which} cycle was never read from the provider")
    return cycle


def _a_month_before(instant: datetime) -> datetime:
    """The same clock time one calendar month earlier, which is what Stripe adds back.

    A subscription's month is calendar arithmetic on the instant it was bought,
    so placing its end requires the same arithmetic backwards. The day is clamped
    where the earlier month is shorter, which is the one case the result comes
    back somewhere other than intended — checked against the period Stripe
    actually opens rather than assumed away.
    """

    month = instant.month - 1 or 12
    year = instant.year - 1 if instant.month == 1 else instant.year
    day = min(instant.day, _days_in(year, month))
    return instant.replace(year=year, month=month, day=day)


def _days_in(year: int, month: int) -> int:
    first = datetime(year, month, 1, tzinfo=UTC)
    following = (
        datetime(year + 1, 1, 1, tzinfo=UTC) if month == 12 else first.replace(month=month + 1)
    )
    return (following - first).days


def _epoch(instant: datetime) -> str:
    return str(int(instant.timestamp()))


def _cleanup(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Settle everything this run made, then delete the clock it was all bound to.

    In that order, because deleting a clock deletes the customer, subscription
    and invoices on it: the report of what the account held has to be taken while
    they are still there.
    """

    report: dict[str, Any]
    try:
        report = run_cleanup(gate.client, gate.admin, run.resources())
    except (InvalidInputError, UpstreamUnavailableError, httpx.HTTPError) as exc:
        report = {"failed": str(exc), "remaining": ["cleanup did not complete"]}
    clock: dict[str, Any] = _delete_the_clock(gate, run)
    report["test_clock"] = clock
    report["remaining"] = _remaining_after_the_clock(report, clock)
    return report


def _remaining_after_the_clock(report: dict[str, Any], clock: dict[str, Any]) -> list[str]:
    """What this run still leaves live, judged on the clock its customer kept.

    The shared cleanup calls an allowance live until its expiry has passed here,
    which is right for an ordinary customer and wrong for this run's: ending a
    grant stamps the customer's own clock, and this run has pushed that clock
    hours past this host's, so an allowance that is over reads as ending later
    today. Re-asked on the clock the expiry was written on, and only then — the
    clock and the customer are both gone by the time this is read, so there is
    nothing left for a live allowance to discount. Every other line the shared
    cleanup produced is kept exactly as it wrote it.
    """

    grants: list[dict[str, Any]] = list(report.get("credit_grants", {}).get("grants", []))
    clock_at = int(clock.get("clock_at") or 0)
    named = {str(grant["id"]) for grant in grants}
    settled = {
        str(grant["id"])
        for grant in grants
        if grant.get("voided_at") is not None
        or (grant.get("expires_at") is not None and int(grant["expires_at"]) <= clock_at)
    }
    remaining: list[str] = []
    for entry in report.get("remaining", []):
        mentioned = {identifier for identifier in named if identifier in str(entry)}
        if mentioned and mentioned <= settled:
            continue
        remaining.append(str(entry))
    if clock["state"] not in {"deleted", "absent"}:
        remaining.append(f"test clock is {clock['state']}")
    return remaining


def _delete_the_clock(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Remove the clock, and with it every object Stripe bound to it.

    A clock mid-advance refuses to be deleted, so its own state is read each
    cycle and the delete is attempted only while it is ready. What proves it gone
    is Stripe no longer answering for it, not the answer to the delete.

    The time it held is carried out with it. Every expiry this run's customer
    carries was stamped on this clock, and once it is deleted there is nothing
    left to read one against.
    """

    if not run.clock_id:
        return {"id": "", "state": "absent", "clock_at": 0}
    deadline = time.monotonic() + CLOCK_DEADLINE_SECONDS
    while True:
        response = gate.client.get(f"/test_helpers/test_clocks/{run.clock_id}")
        if response.status_code == httpx.codes.NOT_FOUND:
            return {"id": run.clock_id, "state": "absent", "clock_at": 0}
        clock = _Clock.model_validate(response.json())
        if clock.status == "ready":
            removed = gate.client.delete(f"/test_helpers/test_clocks/{run.clock_id}")
            print(
                json.dumps(
                    {
                        "at": utc_now().isoformat(timespec="seconds"),
                        "deleting_test_clock": run.clock_id,
                        "clock_status": clock.status,
                        "clock_now": clock.frozen_at.isoformat(),
                        "delete_status": removed.status_code,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if removed.status_code < httpx.codes.BAD_REQUEST:
                return {
                    "id": run.clock_id,
                    "state": "deleted",
                    "clock_at": clock.frozen_time,
                    "clock_now": clock.frozen_at.isoformat(),
                }
        if time.monotonic() >= deadline:
            return {
                "id": run.clock_id,
                "state": clock.status or "present",
                "clock_at": clock.frozen_time,
            }
        time.sleep(CLOCK_POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
