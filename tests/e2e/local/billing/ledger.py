"""Both sides of one run's evidence: what this platform recorded, and what Stripe holds.

The chain from a container to a charge has four links this side of the provider
— the placement the control plane recorded, the segments the pricer priced, the
rows it queued, and the events the scheduler delivered — and one on theirs, which
is what Stripe has actually counted. A scenario that reads only the last one
cannot say which link failed, so every reading here is taken and printed together
whether or not any of them moved.

The provider's own shapes live here too, beside the records they are compared
against. Every scenario in this directory reads the same invoice, the same
allowance and the same delivery, and four private descriptions of one Stripe
object are four things to correct when Stripe adds a field — and four chances for
two scenarios to disagree about what an invoice is.

Nothing here asserts. It reads, it polls, and it says out loud which link is not
holding; what that means is the scenario's to decide.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from database.tables.billing_ledger import (
    BillingLedgerSegmentTable,
    ContainerBillingShapeTable,
)
from database.tables.billing_outbox import BillingMeterOutboxTable
from database.tables.billing_webhook_events import BillingWebhookEventTable
from provider_stripe.api import StripeObject, read
from pydantic import Field, field_validator
from shared.billing_quotes import BilledDimension
from shared.payments import METER_EVENT_NAMES
from shared.timestamps import utc_now
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.e2e.local.billing.gate import BillingGate

COMPUTE_METER = METER_EVENT_NAMES[BilledDimension.ComputeRuntime]
ZERO_RATED_METERS = (
    METER_EVENT_NAMES[BilledDimension.NetworkEgress],
    METER_EVENT_NAMES[BilledDimension.VolumeStorage],
)

EVIDENCE_DEADLINE_SECONDS = 900.0
EVIDENCE_POLL_SECONDS = 5.0
STALL_CYCLES = 2
"""Cycles with nothing moving before the run says so beside the signals.

Not a timeout and not a reason to stop: it is the point at which "nothing is
happening" becomes a finding printed next to the rows that are not moving.
"""


class _Recurring(StripeObject):
    meter: str | None = None


class _LinePrice(StripeObject):
    id: str = ""
    lookup_key: str | None = None
    recurring: _Recurring | None = None


class _PriceDetails(StripeObject):
    price: _LinePrice = Field(default_factory=_LinePrice)

    @field_validator("price", mode="before")
    @classmethod
    def _accept_an_unexpanded_price(cls, value: object) -> object:
        """Stripe sends a bare id where the caller did not expand the object, so
        a reader that only wants the invoice's money need not pay for one."""

        return {"id": value} if isinstance(value, str) else value


class LinePricing(StripeObject):
    price_details: _PriceDetails = Field(default_factory=_PriceDetails)


class PreviewLine(StripeObject):
    id: str = ""
    amount: int = 0
    quantity_decimal: str | None = None
    description: str | None = None
    pricing: LinePricing | None = None

    @property
    def price_name(self) -> str:
        if self.pricing is None:
            return ""
        price = self.pricing.price_details.price
        return price.lookup_key or price.id

    @property
    def metered(self) -> bool:
        return self.pricing is not None and self.pricing.price_details.price.recurring is not None


class PreviewLines(StripeObject):
    data: list[PreviewLine] = Field(default_factory=list)


class PreviewInvoice(StripeObject):
    id: str = ""
    status: str = ""
    total: int = 0
    lines: PreviewLines = Field(default_factory=PreviewLines)


def moment(epoch: int | None) -> str:
    """A Stripe timestamp as an instant, and the empty string for no instant."""

    return datetime.fromtimestamp(epoch, tz=UTC).isoformat() if epoch else ""


class InvoiceSettings(StripeObject):
    default_payment_method: str | None = None


class Customer(StripeObject):
    id: str = ""
    deleted: bool = False
    invoice_settings: InvoiceSettings = Field(default_factory=InvoiceSettings)

    @property
    def default_payment_method(self) -> str:
        return self.invoice_settings.default_payment_method or ""


class SubscriptionItem(StripeObject):
    id: str = ""
    quantity: int = 0
    price: _LinePrice = Field(default_factory=_LinePrice)

    @property
    def price_name(self) -> str:
        return self.price.lookup_key or self.price.id


class SubscriptionItems(StripeObject):
    data: list[SubscriptionItem] = Field(default_factory=list)


class Subscription(StripeObject):
    id: str = ""
    status: str = ""
    latest_invoice: str = ""
    items: SubscriptionItems = Field(default_factory=SubscriptionItems)

    @property
    def price_names(self) -> list[str]:
        return sorted(item.price_name for item in self.items.data)

    def item_for(self, price_lookup_key: str) -> SubscriptionItem | None:
        return next(
            (item for item in self.items.data if item.price.lookup_key == price_lookup_key), None
        )


class SubscriptionList(StripeObject):
    data: list[Subscription] = Field(default_factory=list)


class StatusTransitions(StripeObject):
    finalized_at: int | None = None
    paid_at: int | None = None


class PretaxCredit(StripeObject):
    amount: int = 0
    type: str = ""


class Invoice(StripeObject):
    id: str = ""
    status: str = ""
    billing_reason: str = ""
    attempted: bool = False
    attempt_count: int = 0
    period_start: int = 0
    period_end: int = 0
    subtotal: int = 0
    total: int = 0
    amount_due: int = 0
    amount_paid: int = 0
    next_payment_attempt: int | None = None
    status_transitions: StatusTransitions = Field(default_factory=StatusTransitions)
    total_pretax_credit_amounts: list[PretaxCredit] = Field(default_factory=list)
    lines: PreviewLines = Field(default_factory=PreviewLines)

    @property
    def credit_applied(self) -> int:
        return sum(entry.amount for entry in self.total_pretax_credit_amounts)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "billing_reason": self.billing_reason,
            "period_start": moment(self.period_start),
            "period_end": moment(self.period_end),
            "subtotal_cents": self.subtotal,
            "credit_applied_cents": self.credit_applied,
            "total_cents": self.total,
            "amount_due_cents": self.amount_due,
            "amount_paid_cents": self.amount_paid,
            "charge_attempts": self.attempt_count,
            "next_payment_attempt": moment(self.next_payment_attempt),
            "finalized_at": moment(self.status_transitions.finalized_at),
            "paid_at": moment(self.status_transitions.paid_at),
            "lines": {
                line.price_name: {"quantity": line.quantity_decimal, "amount_cents": line.amount}
                for line in self.lines.data
            },
        }

    def metered_nanos(self, price_lookup_key: str) -> int:
        """One metered line's own quantity, read from the line the customer is
        charged from rather than recomputed from the meter behind it."""

        for line in self.lines.data:
            if line.price_name != price_lookup_key:
                continue
            if line.quantity_decimal is None:
                raise RuntimeError(f"invoice {self.id} bills {price_lookup_key} without a quantity")
            return int(line.quantity_decimal)
        raise RuntimeError(f"invoice {self.id} carries no {price_lookup_key} line")


class InvoiceList(StripeObject):
    data: list[Invoice] = Field(default_factory=list)


class EventObject(StripeObject):
    id: str = ""
    customer: str | None = None


class EventData(StripeObject):
    object: EventObject = Field(default_factory=EventObject)


class Event(StripeObject):
    id: str = ""
    type: str = ""
    created: int = 0
    pending_webhooks: int = 0
    data: EventData = Field(default_factory=EventData)


class EventList(StripeObject):
    data: list[Event] = Field(default_factory=list)


def customer(gate: BillingGate, provider_customer_id: str) -> Customer:
    return read(Customer, gate.client, "GET", f"/customers/{provider_customer_id}")


def subscription(gate: BillingGate, provider_subscription_id: str) -> Subscription:
    return read(Subscription, gate.client, "GET", f"/subscriptions/{provider_subscription_id}")


def invoice(gate: BillingGate, invoice_id: str) -> Invoice:
    return read(
        Invoice,
        gate.client,
        "GET",
        f"/invoices/{invoice_id}",
        params=[("expand[]", "lines.data.pricing.price_details.price")],
    )


def customer_invoices(gate: BillingGate, provider_customer_id: str) -> tuple[Invoice, ...]:
    listed = read(
        InvoiceList,
        gate.client,
        "GET",
        "/invoices",
        params=[
            ("customer", provider_customer_id),
            ("limit", "20"),
            ("expand[]", "data.lines.data.pricing.price_details.price"),
        ],
    )
    return tuple(listed.data)


def event_for_object(
    gate: BillingGate, event_type: str, *, object_id: str, provider_customer_id: str
) -> Event | None:
    """Stripe's own record of a delivery about one of this run's objects."""

    if not object_id:
        return None
    listed = read(
        EventList,
        gate.client,
        "GET",
        "/events",
        params=[("type", event_type), ("limit", "100")],
    )
    for entry in listed.data:
        if entry.data.object.id != object_id:
            continue
        if entry.data.object.customer not in {None, provider_customer_id}:
            continue
        return entry
    return None


def claims(gate: BillingGate, event_ids: Sequence[str]) -> Mapping[str, str]:
    """When this platform durably claimed each delivery, for the ones it did.

    Written in the transaction that acted on the delivery, so it is the only
    signal separating a delivery Stripe believes it made from one this platform
    verified and applied.
    """

    if not event_ids:
        return {}
    with gate.database.session() as session:
        return {
            row.event_id: row.received_at.isoformat()
            for row in session.scalars(
                select(BillingWebhookEventTable).where(
                    BillingWebhookEventTable.event_id.in_(list(event_ids))
                )
            ).all()
        }


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """One meter event the pricer owed, as the durable row states it."""

    identifier: str
    meter_event_name: str
    status: str
    value_nanos: int
    attempts: int
    last_error: str

    def payload(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "meter": self.meter_event_name,
            "status": self.status,
            "value_nanos": self.value_nanos,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class RunLedger:
    """What the platform's own records hold for this run, at one instant."""

    shapes: tuple[str, ...]
    segment_count: int
    segment_cost_nanos: int
    outbox: tuple[OutboxRow, ...]

    @property
    def unsettled(self) -> tuple[OutboxRow, ...]:
        return tuple(row for row in self.outbox if row.status != "sent")

    def sent_identifiers(self, meter_event_name: str) -> tuple[str, ...]:
        return tuple(
            row.identifier
            for row in self.outbox
            if row.status == "sent" and row.meter_event_name == meter_event_name
        )

    def signature(self) -> tuple[Any, ...]:
        return (len(self.shapes), self.segment_count, self.segment_cost_nanos, self.outbox)


@dataclass(frozen=True, slots=True)
class MeteredUsage:
    """Both sides of the comparison, read in the same cycle."""

    ledger: RunLedger
    invoice: PreviewInvoice
    stripe_metered_totals: Mapping[str, int]
    ledger_cost_nanos: int


def drain_metered_usage(
    gate: BillingGate,
    *,
    workspace_id: str,
    provider_subscription_id: str,
) -> MeteredUsage:
    """Poll every signal between the container and the provider, and print all of them.

    Four independent readings a cycle — the recorded placement, the priced
    segments, the queued meter events and what Stripe has actually counted —
    printed whether or not any of them moved. A link that stops moving shows up
    in the next line rather than at a deadline, and the run says so explicitly
    once two cycles pass with nothing changing.

    The loop ends on the whole chain agreeing, never on any one link of it, and
    the deadline is a failure it reports rather than the thing it waits for.
    """

    deadline = time.monotonic() + EVIDENCE_DEADLINE_SECONDS
    previous: tuple[Any, ...] | None = None
    still = 0
    while True:
        with gate.database.session() as session:
            ledger = read_run_ledger(session, workspace_id)
        invoice = preview_invoice(gate, provider_subscription_id)
        totals = gate.provider.invoice_metered_totals(provider_invoice_id=invoice.id)
        owed = ledger_cost_nanos(gate, ledger, COMPUTE_METER)
        cycle: dict[str, Any] = {
            "at": utc_now().isoformat(timespec="seconds"),
            "container_billing_shapes": len(ledger.shapes),
            "ledger_segments": ledger.segment_count,
            "ledger_segment_cost_nanos": ledger.segment_cost_nanos,
            "billing_meter_outbox": [row.payload() for row in ledger.outbox],
            "stripe_metered_totals": dict(totals),
            "ledger_cost_nanos_for_sent_records": owed,
            "draft_invoice": invoice.id,
        }
        signature = (ledger.signature(), tuple(sorted(totals.items())), owed)
        still = still + 1 if signature == previous else 0
        previous = signature
        if still >= STALL_CYCLES:
            cycle["stalled"] = {"cycles": still, "outstanding": outstanding(ledger, totals, owed)}
        print(json.dumps(cycle, sort_keys=True), flush=True)
        if _closed(ledger, totals, owed):
            return MeteredUsage(
                ledger=ledger,
                invoice=invoice,
                stripe_metered_totals=totals,
                ledger_cost_nanos=owed,
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "the chain from container to invoice did not close within "
                f"{EVIDENCE_DEADLINE_SECONDS:.0f}s; last cycle: "
                f"{json.dumps(cycle, sort_keys=True)}"
            )
        time.sleep(EVIDENCE_POLL_SECONDS)


def outstanding(ledger: RunLedger, totals: Mapping[str, int], owed: int) -> list[str]:
    """Which link is not holding, said as the thing to go and look at.

    A stall report that names the wrong link sends the reader after the wrong
    defect, so this is derived from the same readings the cycle printed rather
    than written once as a guess about which step usually hangs.
    """

    unresolved: list[str] = []
    if not ledger.shapes:
        unresolved.append("no container_billing_shapes row: the placement was never recorded")
    if not ledger.segment_cost_nanos:
        unresolved.append("no priced ledger segment: check billing.span.unpriced events")
    if not ledger.outbox:
        unresolved.append("no billing_meter_outbox row: the pricer queued nothing")
    for row in ledger.unsettled:
        unresolved.append(
            f"outbox {row.identifier} is {row.status} after {row.attempts} attempts: "
            f"{row.last_error or 'no error recorded'}"
        )
    counted = totals.get(COMPUTE_METER, 0)
    if not ledger.unsettled and owed and counted != owed:
        unresolved.append(
            f"every event was sent and Stripe has counted {counted} of {owed} nanodollars; "
            "the outstanding step is their own meter aggregation"
        )
    return unresolved


def read_run_ledger(session: Session, workspace_id: str) -> RunLedger:
    shapes = tuple(
        session.scalars(
            select(ContainerBillingShapeTable.container_id).where(
                ContainerBillingShapeTable.workspace_id == workspace_id
            )
        ).all()
    )
    counted, cost = session.execute(
        select(
            func.count(BillingLedgerSegmentTable.id),
            func.coalesce(func.sum(BillingLedgerSegmentTable.cost_nanos), 0),
        ).where(BillingLedgerSegmentTable.workspace_id == workspace_id)
    ).one()
    outbox = tuple(
        OutboxRow(
            identifier=row.identifier,
            meter_event_name=row.meter_event_name,
            status=row.status,
            value_nanos=int(row.value_nanos),
            attempts=int(row.attempts),
            last_error=row.last_error,
        )
        for row in session.scalars(
            select(BillingMeterOutboxTable)
            .where(BillingMeterOutboxTable.workspace_id == workspace_id)
            .order_by(BillingMeterOutboxTable.created_at.asc())
        ).all()
    )
    return RunLedger(
        shapes=shapes,
        segment_count=int(counted),
        segment_cost_nanos=int(cost),
        outbox=outbox,
    )


def ledger_cost_nanos(gate: BillingGate, ledger: RunLedger, meter_event_name: str) -> int:
    """The ledger's own total for exactly the records the provider was sent.

    Summed from the segments rather than from the outbox rows, so the two sides
    of the comparison are the ledger and the invoice rather than the outbox
    twice.
    """

    identifiers = ledger.sent_identifiers(meter_event_name)
    if not identifiers:
        return 0
    with gate.database.session() as session:
        total = session.scalar(
            select(func.coalesce(func.sum(BillingLedgerSegmentTable.cost_nanos), 0)).where(
                BillingLedgerSegmentTable.usage_record_id.in_(identifiers)
            )
        )
    return int(total or 0)


def preview_invoice(gate: BillingGate, provider_subscription_id: str) -> PreviewInvoice:
    """The draft invoice this subscription would produce if it were billed now.

    Built by Stripe from the meter events they have counted, by the same
    machinery that assembles the one a customer is charged from. Reading it
    changes nothing and finalizes nothing.
    """

    return read(
        PreviewInvoice,
        gate.client,
        "POST",
        "/invoices/create_preview",
        data=[("subscription", provider_subscription_id)],
        params=[("expand[]", "lines.data.pricing.price_details.price")],
    )


def _closed(ledger: RunLedger, totals: Mapping[str, int], owed: int) -> bool:
    """Every link holding at once, which is the only state worth stopping on."""

    return bool(
        ledger.shapes
        and ledger.segment_cost_nanos > 0
        and ledger.outbox
        and not ledger.unsettled
        and owed > 0
        and totals.get(COMPUTE_METER) == owed
    )


__all__ = [
    "COMPUTE_METER",
    "ZERO_RATED_METERS",
    "Customer",
    "Event",
    "EventList",
    "Invoice",
    "MeteredUsage",
    "OutboxRow",
    "PreviewInvoice",
    "PreviewLine",
    "RunLedger",
    "Subscription",
    "SubscriptionItem",
    "claims",
    "customer",
    "customer_invoices",
    "drain_metered_usage",
    "event_for_object",
    "invoice",
    "ledger_cost_nanos",
    "moment",
    "outstanding",
    "preview_invoice",
    "read_run_ledger",
    "subscription",
]
