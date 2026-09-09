from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

from api.server.services import ApiServices
from billing.meter_outbox import METER_EVENT_ABANDONED_ACTION, BillingMeterOutboxService
from database.tables.billing_outbox import BillingMeterOutboxTable
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.events import EventLevel
from shared.payments import (
    HostedPaymentSession,
    PaymentCustomer,
    ProviderCreditGrant,
    ProviderCreditGrantBalance,
    ProviderInvoice,
    ProviderPaidSubscriptionPeriod,
    ProviderSubscription,
    SubscriptionChangeTiming,
)
from shared.timestamps import to_utc, utc_now
from sqlalchemy import select
from tests.service_fixtures import workspace_owner_user_id

ACCEPTED = "usage-accepted"
UNREACHABLE = "usage-unreachable"
REFUSED = "usage-refused"


@dataclass(slots=True)
class _Provider:
    """A payment provider that answers differently per event, as one does."""

    accepted: list[str] = field(default_factory=list)

    cards_on_file: set[str] = field(default_factory=set)
    """Customers the provider says hold something chargeable."""

    def record_meter_event(
        self,
        *,
        event_name: str,
        provider_customer_id: str,
        value_nanos: int,
        occurred_at: datetime,
        identifier: str,
        pricing_version: str,
    ) -> None:
        del event_name, provider_customer_id, value_nanos, occurred_at, pricing_version
        if identifier == UNREACHABLE:
            raise UpstreamUnavailableError("the provider is not answering")
        if identifier == REFUSED:
            # The adapter decides which refusals are permanent, and an aged-out
            # timestamp is one of the two that are. A provider that has no meter
            # under the name reports unavailability instead, so that a batch
            # offered to an unpublished account waits rather than abandoning.
            raise InvalidInputError("this usage is older than the provider accepts")
        self.accepted.append(identifier)

    def create_customer(self, *, account_id: str, email: str, workspace_id: str) -> PaymentCustomer:
        raise AssertionError("draining the outbox must not create customers")

    def card_setup_session(
        self, *, provider_customer_id: str, currency: str, success_url: str, cancel_url: str
    ) -> HostedPaymentSession:
        raise AssertionError("draining the outbox must not open hosted pages")

    def customer_portal_session(
        self, *, provider_customer_id: str, return_url: str
    ) -> HostedPaymentSession:
        raise AssertionError("draining the outbox must not open hosted pages")

    def payment_method_owner(self, *, provider_payment_method_id: str) -> str:
        raise AssertionError("draining the outbox must not read cards")

    def has_payment_method(self, *, provider_customer_id: str) -> bool:
        return provider_customer_id in self.cards_on_file

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        raise AssertionError("draining the outbox must not change cards")

    def create_subscription(
        self, *, provider_customer_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        raise AssertionError("draining the outbox must not subscribe anyone")

    def set_subscription_plan(
        self,
        *,
        provider_subscription_id: str,
        terms_version: SubscriptionTermsVersion,
        timing: SubscriptionChangeTiming,
        operation_id: str,
        operation_created_at: datetime,
    ) -> ProviderSubscription:
        raise AssertionError("draining the outbox must not change anyone's plan")

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        raise AssertionError("draining the outbox must not read subscriptions")

    def create_credit_grant(
        self,
        *,
        account_id: str,
        provider_customer_id: str,
        amount_nanos: int,
        period_ended_at: datetime,
        previous_period_ended_at: datetime | None,
    ) -> ProviderCreditGrant:
        raise AssertionError("draining the outbox must not grant an allowance")

    def expire_credit_grant(self, *, provider_credit_grant_id: str) -> None:
        raise AssertionError("draining the outbox must not expire an allowance")

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        raise AssertionError("draining the outbox must not read invoices")

    def credit_grants_for(
        self, *, provider_customer_id: str
    ) -> Sequence[ProviderCreditGrantBalance]:
        return ()

    def paid_subscription_periods(
        self, *, provider_customer_id: str, provider_subscription_id: str, since: datetime
    ) -> Sequence[ProviderPaidSubscriptionPeriod]:
        return ()

    def invoices_for(
        self, *, provider_customer_id: str, since: datetime, limit: int | None = 12
    ) -> Sequence[ProviderInvoice]:
        raise AssertionError("draining the outbox must not list invoices")


def test_a_refused_meter_event_is_settled_alone_and_holds_up_nothing_behind_it(
    isolated_services: ApiServices,
) -> None:
    """The property the outbox exists for.

    A sweep that recomputes what it owes on every run puts the same failing
    account at the front of every batch, and everything behind it waits for an
    account that will never succeed. Here each row carries its own outcome: the
    reachable one is acknowledged, the refused one is paced into the future so
    the next claim passes over it, and the one the provider will never accept is
    abandoned with a durable record of the money that never left — all from a
    single sweep over one batch.
    """

    now = utc_now()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    workspace_owner_user_id(isolated_services.context, workspace_id)
    _enqueue(
        isolated_services,
        workspace_id=workspace_id,
        identifiers=(ACCEPTED, UNREACHABLE, REFUSED),
        now=now,
    )

    provider = _Provider()
    service = BillingMeterOutboxService(
        database=isolated_services.context.database,
        payments=lambda: provider,
        events=isolated_services.events,
    )
    result = service.drain(now=now)
    backlog = service.abandoned_backlog()

    assert provider.accepted == [ACCEPTED]
    assert (result.sent_count, result.retried_count, result.abandoned_count) == (1, 1, 1)
    # The standing backlog, which is what an operator watches: the deltas above
    # are gone after this tick and the row is never pruned, so a charge given up
    # on is only ever visible as this figure. Asked of the outbox rather than
    # returned by the sweep, because the provider is what makes a sweep fail and
    # a figure that went quiet then would go quiet exactly when it is needed.
    assert (backlog.count, backlog.value_nanos) == (1, 1_500)

    rows = _rows(isolated_services)
    assert rows[ACCEPTED].status == "sent"
    assert rows[REFUSED].status == "abandoned"
    refused_again = rows[UNREACHABLE]
    assert refused_again.status == "pending"
    assert refused_again.attempts == 1
    assert to_utc(refused_again.next_attempt_at) > now
    assert all(row.claim_token is None for row in rows.values())

    abandonment = isolated_services.events.list(
        workspace_id=workspace_id, actions=[METER_EVENT_ABANDONED_ACTION]
    )
    assert [(event.level, event.resource_id) for event in abandonment] == [
        (EventLevel.Error, REFUSED)
    ]


def _enqueue(
    services: ApiServices,
    *,
    workspace_id: str,
    identifiers: tuple[str, ...],
    now: datetime,
) -> None:
    with services.context.database.session() as session:
        for identifier in identifiers:
            session.add(
                BillingMeterOutboxTable(
                    id=str(uuid4()),
                    workspace_id=workspace_id,
                    identifier=identifier,
                    usage_record_id=str(uuid4()),
                    provider_customer_id="cus_outbox",
                    meter_event_name="lazycloud_compute_cost_nanos",
                    value_nanos=1_500,
                    pricing_version="2026-08-13.a",
                    occurred_at=now - timedelta(minutes=1),
                    metering_ended_at=now,
                    status="pending",
                    attempts=0,
                    next_attempt_at=now - timedelta(seconds=1),
                )
            )


def _rows(services: ApiServices) -> dict[str, BillingMeterOutboxTable]:
    with services.context.database.session() as session:
        return {
            row.identifier: row for row in session.scalars(select(BillingMeterOutboxTable)).all()
        }
