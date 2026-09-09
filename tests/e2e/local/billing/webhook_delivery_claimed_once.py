"""Prove a real Stripe delivery is acted on, and that a repeat of it is not.

The webhook endpoint is the only way anything Stripe decides gets back into this
platform, and it is the only route with no bearer token on it: the signature is
the whole of the authorization. Nothing else in this repository proves that a
delivery Stripe actually sent — signed by them, over the public URL, through
whatever sits in front of the control plane — is verified, claimed and applied.

The delivery is caused rather than constructed. The run registers a customer
through the card route, attaches Stripe's test card to it, and that attach is
what makes Stripe send `payment_method.attached` to the endpoint. The durable
effect is the one the card-saved handler exists for: the saved card becomes the
customer's default, which is a fact readable at Stripe rather than an
acknowledgement readable here.

Then a second card is attached, and the platform makes that one the default the
same way. Only now is the *first* delivery replayed from Stripe. That ordering is
the whole point: if a replayed event were applied a second time it would put the
old card back, so the default staying on the second card is the claim refusing —
observable in what a customer would be charged on next month, not merely in a
row that says "seen".

Registering the customer also puts the account on the free plan, so this run's
customer holds a subscription, a grant and the invoices that come with them even
though none of that is its subject. All of it is named to cleanup, because a
scenario that leaves a live subscription behind has left one whatever it was
about.

Three independent signals are read every cycle and printed whether or not they
moved: Stripe's own `/v1/events`, including how many endpoints still owe a
successful delivery for it; the control plane's own record of the requests that
reached it; and the durable claim and the customer at Stripe.

```sh
uv run python -m tests.e2e.local.billing.webhook_delivery_claimed_once --live \
  --confirm-account acct_...
```

Everything the run creates is removed by `tests.e2e.local.billing.cleanup`, which
this module calls on its way out. The webhook endpoint is read and never
modified: it belongs to the account.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from database.tables.billing_webhook_events import BillingWebhookEventTable
from foundation.environment_file import load_environment_file
from provider_stripe.api import StripeObject, read
from pydantic import Field
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.timestamps import utc_now
from sqlalchemy import func, select
from tests.e2e._support.process import LivePrerequisiteError, blocked, run_text_process
from tests.e2e.local.billing.cleanup import RunResources, run_cleanup
from tests.e2e.local.billing.gate import (
    REPLACEMENT_PAYMENT_METHOD,
    TEST_PAYMENT_METHOD,
    BillingGate,
    RunAccount,
    attach_card,
    billing_gate,
    create_run_account,
    register_customer,
)
from tests.e2e.local.billing.ledger import (
    Event,
    EventList,
    claims,
    customer,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]

WEBHOOK_PATH = "/webhooks/stripe"
CARD_SAVED_EVENT = "payment_method.attached"

DELIVERY_DEADLINE_SECONDS = 240.0
DELIVERY_POLL_SECONDS = 3.0
REPLAY_OBSERVATION_CYCLES = 4
"""Cycles to keep watching after a replay has demonstrably been delivered.

A second application would land in the same instant the delivery does, so this
is not a wait for one — it is enough readings to say the state held rather than
that it had not moved yet.
"""

LOG_READ_TIMEOUT_SECONDS = 60.0


class _WebhookEndpoint(StripeObject):
    id: str = ""
    url: str = ""
    status: str = ""
    enabled_events: list[str] = Field(default_factory=list)


class _WebhookEndpointList(StripeObject):
    data: list[_WebhookEndpoint] = Field(default_factory=list)


@dataclass(slots=True)
class _Run:
    account: RunAccount
    provider_customer_id: str = ""
    provider_subscription_id: str = ""
    endpoint_id: str = ""
    first_card: str = ""
    second_card: str = ""
    first_event: str = ""
    second_event: str = ""
    log_since: str = ""

    def resources(self) -> RunResources:
        return RunResources(
            user_id=self.account.user_id,
            workspace_id=self.account.workspace_id,
            provider_customer_id=self.provider_customer_id,
            provider_subscription_id=self.provider_subscription_id,
        )

    def identifiers(self) -> dict[str, str]:
        return {
            "workspace_id": self.account.workspace_id,
            "workspace_name": self.account.workspace_name,
            "user_id": self.account.user_id,
            "customer_id": self.provider_customer_id,
            "subscription_id": self.provider_subscription_id,
            "webhook_endpoint_id": self.endpoint_id,
            "first_card_event": self.first_event,
            "second_card_event": self.second_event,
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
        endpoint_id = _require_endpoint(gate)
    except LivePrerequisiteError as exc:
        return blocked(exc)
    return _scenario(gate, endpoint_id)


def _require_endpoint(gate: BillingGate) -> str:
    """The account's endpoint for this platform, found by this platform's own URL.

    Named by where it points rather than by an identifier written down here: an
    id in the source is one nobody notices has gone stale, and the thing that has
    to be true is that deliveries for this account arrive at this deployment.
    """

    expected = f"{gate.public_url.rstrip('/')}{WEBHOOK_PATH}"
    listed = read(
        _WebhookEndpointList, gate.client, "GET", "/webhook_endpoints", params=[("limit", "100")]
    )
    for endpoint in listed.data:
        if endpoint.url != expected:
            continue
        if endpoint.status != "enabled":
            raise LivePrerequisiteError(f"the endpoint at {expected} is {endpoint.status}")
        if CARD_SAVED_EVENT not in endpoint.enabled_events:
            raise LivePrerequisiteError(
                f"the endpoint at {expected} does not carry {CARD_SAVED_EVENT}, "
                "which is the delivery this scenario causes"
            )
        return endpoint.id
    raise LivePrerequisiteError(
        f"this account has no webhook endpoint pointing at {expected}; "
        "apply deploy/stripe against it first"
    )


def _scenario(gate: BillingGate, endpoint_id: str) -> int:
    run = _Run(account=RunAccount(suffix=secrets.token_hex(6)), endpoint_id=endpoint_id)
    run.log_since = _log_timestamp()
    evidence: dict[str, Any] = {}
    primary: BaseException | None = None
    try:
        create_run_account(gate, run.account)
        _register_customer(gate, run)
        evidence["first_card"] = _save_card(gate, run, TEST_PAYMENT_METHOD, first=True)
        evidence["second_card"] = _save_card(gate, run, REPLACEMENT_PAYMENT_METHOD, first=False)
        evidence["replay"] = _replay_first_delivery(gate, run)
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


def _register_customer(gate: BillingGate, run: _Run) -> None:
    """Register the customer the way the product does, from the card route."""

    account = register_customer(gate, run.account)
    run.provider_customer_id = account.provider_customer_id
    run.provider_subscription_id = account.provider_subscription_id


def _save_card(gate: BillingGate, run: _Run, token: str, *, first: bool) -> dict[str, Any]:
    """Attach a card at Stripe and wait for the platform to act on their delivery.

    Attaching is the customer's half of the hosted page, which no local
    automation can drive. Everything after it is the product's: Stripe raises the
    event, delivers it signed, and the platform's own handler makes the card the
    default. Nothing here sets a default.
    """

    method_id = attach_card(gate, run.provider_customer_id, token=token)
    if first:
        run.first_card = method_id
    else:
        run.second_card = method_id
    event = _poll(
        gate,
        run,
        label=f"attach {token}",
        done=lambda state: (
            state.event is not None
            and state.event.pending_webhooks == 0
            and state.event.id in state.claims
            and state.default_payment_method == method_id
        ),
        find_event=lambda: _event_for_payment_method(gate, method_id, run.provider_customer_id),
    )
    if first:
        run.first_event = event.id
    else:
        run.second_event = event.id
    return {
        "payment_method": method_id,
        "event": event.id,
        "event_type": event.type,
        "pending_webhooks": event.pending_webhooks,
        "default_payment_method_at_stripe": method_id,
        "claim": dict(claims(gate, [event.id])),
    }


def _replay_first_delivery(gate: BillingGate, run: _Run) -> dict[str, Any]:
    """Make Stripe send the first delivery again, and prove nothing moved.

    Replayed from Stripe rather than posted here: a body this run signed would
    prove only that this run can sign, where a replay exercises the same
    signature, the same public route and the same handler that the original did.

    Arrival is established as a request that produced no claim, rather than as a
    request at all. This run's own account holds a subscription, so Stripe has
    other deliveries in flight about it — but every one of those writes a claim
    row when it lands, so each adds one to both counts and leaves the difference
    alone. Only a delivery the platform refused to act on twice can raise it.
    """

    before_claims = claims(gate, [run.first_event])
    if len(before_claims) != 1:
        raise RuntimeError(
            f"{run.first_event} is claimed {len(before_claims)} times before any replay"
        )
    # Both counts restart at the replay, so neither carries the provisioning
    # deliveries this run caused before it.
    replayed_at = utc_now()
    run.log_since = _log_timestamp()
    replayed = read(
        Event,
        gate.client,
        "POST",
        f"/events/{run.first_event}/retry",
        data=[("webhook_endpoint", run.endpoint_id)],
    )
    held = 0
    deadline = time.monotonic() + DELIVERY_DEADLINE_SECONDS
    while True:
        state = _read_state(gate, run, event=_event(gate, run.first_event))
        unclaimed = state.deliveries - _claims_recorded_since(gate, replayed_at)
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "replayed_event": run.first_event,
            "pending_webhooks": state.event.pending_webhooks if state.event else None,
            "requests_reaching_the_control_plane_since_the_replay": state.deliveries,
            "claims_recorded_since_the_replay": _claims_recorded_since(gate, replayed_at),
            "requests_that_claimed_nothing": unclaimed,
            "claims_for_replayed_event": dict(state.claims),
            "default_payment_method_at_stripe": state.default_payment_method,
            "held_cycles": held,
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        _assert_untouched(run, state, before_claims)
        if unclaimed >= 1 and state.event is not None and state.event.pending_webhooks == 0:
            held += 1
            if held >= REPLAY_OBSERVATION_CYCLES:
                return {
                    "replayed_event": run.first_event,
                    "stripe_accepted_the_replay": replayed.id == run.first_event,
                    "requests_since_the_replay": state.deliveries,
                    "claims_recorded_since_the_replay": _claims_recorded_since(gate, replayed_at),
                    "requests_that_claimed_nothing": unclaimed,
                    "pending_webhooks_after_delivery": state.event.pending_webhooks,
                    "claims_for_replayed_event": dict(state.claims),
                    "default_payment_method_at_stripe": state.default_payment_method,
                    "second_card": run.second_card,
                    "first_card": run.first_card,
                    "applied_twice": False,
                }
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"the replay of {run.first_event} was not delivered within "
                f"{DELIVERY_DEADLINE_SECONDS:.0f}s; last cycle: {json.dumps(cycle, sort_keys=True)}"
            )
        time.sleep(DELIVERY_POLL_SECONDS)


def _assert_untouched(run: _Run, state: _State, before: Mapping[str, str]) -> None:
    """Fail the instant a replay changes anything, rather than at the end.

    Checked every cycle because the failure this guards against is transient in
    the worst way: a second application sets the default back and a third could
    set it forward again, and a run that only looked once at the end would call
    that a pass.
    """

    if dict(state.claims) != dict(before):
        raise RuntimeError(
            f"the replay of {run.first_event} changed the claim: {dict(before)} became "
            f"{dict(state.claims)}"
        )
    if state.default_payment_method != run.second_card:
        raise RuntimeError(
            f"the replay of {run.first_event} moved the default payment method to "
            f"{state.default_payment_method}; the platform applied a delivery it had already "
            "acted on"
        )


@dataclass(frozen=True, slots=True)
class _State:
    """Every signal, read in one cycle."""

    event: Event | None = None
    claims: Mapping[str, str] = field(default_factory=lambda: {})
    default_payment_method: str = ""
    deliveries: int = 0


def _poll(
    gate: BillingGate,
    run: _Run,
    *,
    label: str,
    done: Callable[[_State], bool],
    find_event: Callable[[], Event | None],
) -> Event:
    """Read every signal each cycle, print them, and stop when they all agree."""

    deadline = time.monotonic() + DELIVERY_DEADLINE_SECONDS
    while True:
        state = _read_state(gate, run, event=find_event())
        cycle: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "step": label,
            "event": state.event.id if state.event else "",
            "pending_webhooks": state.event.pending_webhooks if state.event else None,
            "requests_reaching_the_control_plane": state.deliveries,
            "claims": dict(state.claims),
            "default_payment_method_at_stripe": state.default_payment_method,
        }
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if done(state) and state.event is not None:
            return state.event
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"{label} did not reach the platform within {DELIVERY_DEADLINE_SECONDS:.0f}s; "
                f"last cycle: {json.dumps(cycle, sort_keys=True)}"
            )
        time.sleep(DELIVERY_POLL_SECONDS)


def _read_state(gate: BillingGate, run: _Run, *, event: Event | None) -> _State:
    return _State(
        event=event,
        claims=claims(gate, [event.id]) if event is not None else {},
        default_payment_method=customer(gate, run.provider_customer_id).default_payment_method,
        deliveries=_delivered_requests(run),
    )


def _event(gate: BillingGate, event_id: str) -> Event:
    return read(Event, gate.client, "GET", f"/events/{event_id}")


def _event_for_payment_method(
    gate: BillingGate, payment_method_id: str, provider_customer_id: str
) -> Event | None:
    """Stripe's own record of the delivery this run caused, matched on its object."""

    listed = read(
        EventList,
        gate.client,
        "GET",
        "/events",
        params=[("type", CARD_SAVED_EVENT), ("limit", "100")],
    )
    for event in listed.data:
        if (
            event.data.object.id == payment_method_id
            and event.data.object.customer == provider_customer_id
        ):
            return event
    return None


def _claims_recorded_since(gate: BillingGate, moment: datetime) -> int:
    """How many deliveries this platform durably acted on since an instant.

    Counted across every account rather than this run's, because it is subtracted
    from a request count that is also every account's: the two have to be asked
    the same question for their difference to mean anything.
    """

    with gate.database.session() as session:
        return int(
            session.scalar(
                select(func.count(BillingWebhookEventTable.event_id)).where(
                    BillingWebhookEventTable.received_at >= moment
                )
            )
            or 0
        )


def _delivered_requests(run: _Run) -> int:
    """How many requests have reached the control plane's webhook route.

    The receiving end of the same question Stripe answers from theirs. Both are
    read because they fail differently: Stripe reports a delivery it believes it
    made, and this reports one the process actually served — and a replay that
    never left Stripe would look identical to one that arrived and was correctly
    ignored, in every signal but this one.
    """

    result = run_text_process(
        ["docker", "compose", "logs", "control-plane", "--since", run.log_since],
        cwd=REPOSITORY_ROOT,
        timeout=LOG_READ_TIMEOUT_SECONDS,
    )
    return sum(1 for line in result.stdout.splitlines() if f"POST {WEBHOOK_PATH}" in line)


def _log_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cleanup(gate: BillingGate, run: _Run) -> dict[str, Any]:
    try:
        return run_cleanup(gate.client, gate.admin, run.resources())
    except (InvalidInputError, UpstreamUnavailableError, httpx.HTTPError) as exc:
        return {"failed": str(exc), "remaining": ["cleanup did not complete"]}


if __name__ == "__main__":
    raise SystemExit(main())
