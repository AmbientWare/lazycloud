"""Prove the card gate: an account nobody can bill is stopped, and a card frees it.

The half of billing that is not bookkeeping. Everything else in this directory
proves money is measured and invoiced correctly; this proves the platform stops
spending money on somebody who has given it no way to collect — and, just as
important, that attaching a card undoes that immediately rather than next month.

Nothing here is simulated. The account is registered by the product's own sign-in
path and starts with no card, which is what every real account does. Its cycle is
opened with an allowance of zero so that real metered usage crosses the line in
seconds rather than in the hours a dollar of compute takes — the figure a cycle
opens with is the one knob this run turns, and everything downstream of it is
production: real containers, real usage records, the real pricer, the real
scheduler sweep, and a real card attached at Stripe.

Four things must hold, in order:

1. A cardless account is admitted while it still has allowance. Without this the
   refusal later proves only that something is broken.
2. Once the allowance is spent, the running container is stopped by the sweep —
   with `UNFUNDED` as its reason, not `USER`, because the customer is owed an
   explanation and a wrong one is worse than none.
3. New work is refused with `402` while it stays cardless.
4. Attaching a card raises the allowance to what the plan includes, in the cycle
   already in progress, and work runs again.

```sh
uv run python -m tests.e2e.local.billing.card_gate_stops_and_resumes --live \
  --confirm-account acct_...
```

Everything the run creates is removed by `tests.e2e.local.billing.cleanup`, which
this module calls on its way out.
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

from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.orchestration import ContainerRepository
from foundation.environment_file import load_environment_file
from lazycloud.abstractions.function import Function, FunctionOperationError
from lazycloud.config import reset_settings_cache
from lazycloud.control import control_workspace_scope
from shared.billing_rate_card import FREE_PLAN_INCLUDED_NANOS, NO_CARD_INCLUDED_NANOS
from shared.container_requests import StopContainerReason
from shared.http.billing import BillingSummaryResponse
from shared.http.errors import HttpApiError
from shared.timestamps import utc_now
from tests.e2e._support.process import LivePrerequisiteError, blocked
from tests.e2e.local.billing.cleanup import RunResources, run_cleanup
from tests.e2e.local.billing.gate import (
    TEST_PAYMENT_METHOD,
    BillingGate,
    RunAccount,
    attach_card,
    billing_account,
    billing_gate,
    create_run_account,
    plan_allowance,
    register_customer,
)

SOURCE_ROOT = Path(__file__).resolve().parent

CLIENT_TIMEOUT_SECONDS = 900.0
SUMMARY_ROUTE = "/api/v1/billing/summary"

ADMITTED_HOLD_SECONDS = 3.0
TASK_TIMEOUT_SECONDS = 300
BURN_HOLD_SECONDS = 120.0
"""How long the container asked to run holds for.

Long enough that the sweep has to be what ends it. A container that finished on
its own would leave nothing to prove: the interesting outcome is one killed
part-way through, which is what an account running out of money looks like.
"""

STOP_DEADLINE_SECONDS = 300.0
CARD_DEADLINE_SECONDS = 240.0
POLL_SECONDS = 3.0

PAYMENT_REQUIRED_STATUS = 402

BRINK_MARGIN_NANOS = 100_000
"""What is left unspent before the container that crosses the line is started.

Enough that admission admits it — a cardless account with nothing remaining is
refused, so a run that spent every nanodollar could never start the work meant to
take it past zero.

Small enough that the *first* usage report clears it, which is the constraint
that matters: one window of a modest container prices a few hundred thousand
nanodollars, so a margin near that is one the container might never cross before
its hold ends, and the run would time out waiting for a sweep that was right not
to fire.
"""


@dataclass(slots=True)
class _Run:
    account: RunAccount
    provider_customer_id: str = ""
    provider_subscription_id: str = ""
    provider_credit_grant_id: str = ""
    card: str = ""

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
            "card": self.card,
        }


def main(argv: Sequence[str] | None = None) -> int:
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
        evidence["registered_without_a_card"] = _register(gate, run)
        function = _deploy(gate, run)
        evidence["admitted_with_allowance"] = _admitted(run, function)
        evidence["taken_to_the_brink"] = _spend_almost_everything(gate, run, function)
        evidence["stopped_by_the_sweep"] = _stopped(gate, run)
        evidence["refused_while_cardless"] = _refused(run, function)
        evidence["card_restores_the_plan"] = _attach_a_card(gate, run)
        evidence["admitted_again"] = _admitted(run, function)
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


def _register(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Sign the account in, and confirm it starts on the cardless terms.

    Nothing is done to make it cardless: that is what registration leaves, and
    the whole gate rests on the platform knowing it without asking Stripe.
    """

    account = register_customer(gate, run.account)
    run.provider_customer_id = account.provider_customer_id
    run.provider_subscription_id = account.provider_subscription_id
    run.provider_credit_grant_id = account.provider_credit_grant_id
    if account.payment_method_attached_at is not None:
        raise RuntimeError("a freshly registered account already claims a card on file")
    summary = _summary(gate, run)
    allowance = plan_allowance(summary)
    if summary.payment_method_on_file:
        raise RuntimeError("the summary claims a card on file for an account that has none")
    if allowance.allowance_nanos != NO_CARD_INCLUDED_NANOS:
        raise RuntimeError(
            f"the cycle opened with {allowance.allowance_nanos} rather than the cardless "
            f"{NO_CARD_INCLUDED_NANOS}"
        )
    return {
        "payment_method_on_file": summary.payment_method_on_file,
        "allowance_nanos": allowance.allowance_nanos,
        "max_concurrent_containers": _container_limit(summary),
    }


def _deploy(gate: BillingGate, run: _Run) -> Function[..., dict[str, float]]:
    from tests.e2e.local.billing.workload_metered_container import metered_container

    metered_container.endpoint = gate.endpoint
    metered_container.token = run.account.token
    metered_container.timeout = CLIENT_TIMEOUT_SECONDS
    reset_settings_cache()
    with control_workspace_scope(run.account.workspace_name):
        metered_container.deploy(workspace=run.account.workspace_name, source_root=SOURCE_ROOT)
    return metered_container


def _admitted(run: _Run, function: Function[..., dict[str, float]]) -> dict[str, Any]:
    """The control, run twice: once before the gate closes and once after it opens."""

    with control_workspace_scope(run.account.workspace_name):
        call = function.spawn(hold_seconds=ADMITTED_HOLD_SECONDS)
        observed = call.get(timeout_seconds=TASK_TIMEOUT_SECONDS, poll_interval_seconds=1)
    return {"task_id": call.task_id, "held_seconds": float(observed["held_seconds"])}


def _spend_almost_everything(
    gate: BillingGate, run: _Run, function: Function[..., dict[str, float]]
) -> dict[str, Any]:
    """Bring the account to the brink, then start work that carries it over.

    The one value this run sets by hand, and it is the spend rather than the
    terms: the cycle keeps the real cardless allowance, and what is moved is how
    much of it has already gone. A dollar of compute takes hours to burn at the
    published rates, and what is being proved is not the arithmetic — that is
    `metered_invoice_settled`'s job — but what happens at the moment the line is
    crossed.

    A margin is left rather than nothing at all. Admission refuses a cardless
    account with nothing remaining, so an account taken to exactly zero could
    never start the container meant to take it past zero — the run would prove
    the refusal and never reach the sweep. The margin is small enough that one
    metering window of real usage clears it.
    """

    with gate.database.session() as session:
        allowances = BillingAllowanceRepository(session)
        period = allowances.current_period(user_id=run.account.user_id, at=utc_now())
        if period is None:
            raise RuntimeError("the account holds no cycle to spend against")
        allowances.increment(
            user_id=run.account.user_id,
            at=utc_now(),
            cost_nanos=max(period.remaining_nanos - BRINK_MARGIN_NANOS, 0),
        )
        session.commit()
    with gate.database.session() as session:
        left = BillingAllowanceRepository(session).current_period(
            user_id=run.account.user_id, at=utc_now()
        )
    with control_workspace_scope(run.account.workspace_name):
        call = function.spawn(hold_seconds=BURN_HOLD_SECONDS)
    return {
        "task_id": call.task_id,
        "remaining_nanos_before_start": left.remaining_nanos if left else None,
    }


def _live_containers(gate: BillingGate, run: _Run) -> tuple[str, ...]:
    with gate.database.session() as session:
        return tuple(
            ContainerRepository(session).live_container_ids_for_owner(
                owner_user_id=run.account.user_id, limit=50
            )
        )


def _stop_reasons(gate: BillingGate, run: _Run) -> dict[str, str]:
    with gate.database.session() as session:
        records = ContainerRepository(session).list(workspace_id=run.account.workspace_id)
    return {
        record.id: (record.termination_reason or "")
        for record in records
        if record.termination_reason
    }


def _stopped(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Poll until the sweep has stopped what the account can no longer pay for.

    Polled rather than waited on, and every cycle prints what it read: a sweep
    that never fires and a container that never started look identical from a
    deadline, and only one of them is a bug in the thing under test.
    """

    deadline = time.monotonic() + STOP_DEADLINE_SECONDS
    while True:
        live = _live_containers(gate, run)
        reasons = _stop_reasons(gate, run)
        unfunded = {
            container_id: reason
            for container_id, reason in reasons.items()
            if reason == StopContainerReason.Unfunded.value
        }
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "live_containers": list(live),
            "termination_reasons": reasons,
            "unfunded": list(unfunded),
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if unfunded and not live:
            return {"stopped": list(unfunded), "reasons": reasons}
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"nothing was stopped for an unfunded account within {STOP_DEADLINE_SECONDS:.0f}s; "
                f"last cycle: {json.dumps(cycle, sort_keys=True)}"
            )
        time.sleep(POLL_SECONDS)


def _refused(run: _Run, function: Function[..., dict[str, float]]) -> dict[str, Any]:
    """New work must be refused, and refused as a payment problem.

    The status is the contract: a customer who can fix this by adding a card has
    to be told that, and a 500 or a silently failed task tells them their code is
    broken instead.
    """

    with control_workspace_scope(run.account.workspace_name):
        try:
            function.spawn(hold_seconds=ADMITTED_HOLD_SECONDS)
        except FunctionOperationError as exc:
            # The SDK wraps the transport failure before a caller sees it, so the
            # status is on the cause. Read through rather than matched on the
            # message: the words are a product decision and may be reworded, and
            # the status is the contract.
            cause = exc.__cause__
            status = cause.status_code if isinstance(cause, HttpApiError) else None
            if status != PAYMENT_REQUIRED_STATUS:
                raise RuntimeError(
                    f"a cardless account past its allowance was refused with {status}, "
                    f"not {PAYMENT_REQUIRED_STATUS}"
                ) from exc
            return {"status_code": status, "message": str(exc)}
    raise RuntimeError("a cardless account past its allowance was allowed to start new work")


def _attach_a_card(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Attach a card and wait for the platform's own handler to act on it.

    Attaching is all a customer does; everything after it is production code
    reacting to the delivery. What must arrive is not just the column but the
    replacement grant — the cycle already in progress has to be re-termed onto
    the plan's allowance, or the customer has paid to keep waiting.
    """

    run.card = attach_card(gate, run.provider_customer_id, token=TEST_PAYMENT_METHOD)
    deadline = time.monotonic() + CARD_DEADLINE_SECONDS
    while True:
        summary = _summary(gate, run)
        allowance = summary.plan.allowance if summary.plan else None
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "card": run.card,
            "payment_method_on_file": summary.payment_method_on_file,
            "allowance_nanos": allowance.allowance_nanos if allowance else None,
            "max_concurrent_containers": _container_limit(summary),
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if (
            summary.payment_method_on_file
            and allowance is not None
            and allowance.allowance_nanos == FREE_PLAN_INCLUDED_NANOS
        ):
            account = billing_account(gate, run.account)
            if account is not None:
                run.provider_credit_grant_id = account.provider_credit_grant_id
            return {
                "payment_method_on_file": True,
                "allowance_nanos": allowance.allowance_nanos,
                "max_concurrent_containers": _container_limit(summary),
                "credit_grant_id": run.provider_credit_grant_id,
            }
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"attaching a card did not restore the plan's terms within "
                f"{CARD_DEADLINE_SECONDS:.0f}s; last cycle: {json.dumps(cycle, sort_keys=True)}"
            )
        time.sleep(POLL_SECONDS)


def _summary(gate: BillingGate, run: _Run) -> BillingSummaryResponse:
    return BillingSummaryResponse.model_validate(run.account.channel(gate).get(SUMMARY_ROUTE))


def _container_limit(summary: BillingSummaryResponse) -> int:
    if summary.entitlements is None:
        raise RuntimeError("the subscribed account summary has no plan entitlements")
    return summary.entitlements.max_concurrent_containers


def _cleanup(gate: BillingGate, run: _Run) -> dict[str, Any]:
    return run_cleanup(gate.client, gate.admin, run.resources())


if __name__ == "__main__":
    raise SystemExit(main())
