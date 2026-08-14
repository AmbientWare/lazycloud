from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import sleep
from uuid import uuid4

import pytest
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_outbox import BillingMeterOutboxRepository
from database.repositories.identity import UserRepository, WorkspaceRepository
from database.tables.billing_outbox import BillingMeterOutboxTable
from database.tables.billing_rates import ComputeRateTable, PlatformRateTable
from shared.billing_accounts import BillingAccount
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import FREE_PLAN_INCLUDED_NANOS
from shared.payments import (
    HostedPaymentSession,
    PaymentCustomer,
    ProviderCreditGrant,
    ProviderSubscription,
)
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from billing import BillingAccountService
from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

# Every one of these invariants is enforced by PostgreSQL and by nothing in
# Python, so the in-memory SQLite the rest of this package's tests run on accepts
# the state each of them exists to make impossible: it has no range exclusion
# constraint, it ignores `FOR UPDATE` and `FOR UPDATE SKIP LOCKED`, and it
# serializes sessions behind one lock so no two writers ever meet.

CYCLE_STARTED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
CYCLE_ENDED_AT = datetime(2026, 9, 13, 9, 30, tzinfo=UTC)


@dataclass(slots=True)
class _RegistrationCountingProvider:
    """A payment provider that hands out a new object to every caller.

    Deliberately not idempotent, so a second registration, subscription or grant
    is visible as a second object rather than hidden behind the keys the real
    adapter sends. Those keys are the other half of this protection and expire
    after a day; the lock this exercises is the half that does not.
    """

    registrations: list[str] = field(default_factory=list)
    subscribes: list[str] = field(default_factory=list)
    grants: list[str] = field(default_factory=list)

    def create_customer(self, *, account_id: str, email: str, workspace_id: str) -> PaymentCustomer:
        del email, workspace_id
        self.registrations.append(account_id)
        return PaymentCustomer(provider_customer_id=f"cus_{len(self.registrations)}")

    def card_setup_session(
        self, *, provider_customer_id: str, currency: str, success_url: str, cancel_url: str
    ) -> HostedPaymentSession:
        raise AssertionError("registering must not open a card page")

    def customer_portal_session(
        self, *, provider_customer_id: str, return_url: str
    ) -> HostedPaymentSession:
        raise AssertionError("registering must not open a portal page")

    def payment_method_owner(self, *, provider_payment_method_id: str) -> str:
        raise AssertionError("registering must not read payment methods")

    def set_default_payment_method(
        self, *, provider_customer_id: str, provider_payment_method_id: str
    ) -> None:
        raise AssertionError("registering must not set a default card")

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
        raise AssertionError("registering must not meter usage")

    def create_subscription(
        self, *, provider_customer_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        self.subscribes.append(provider_customer_id)
        return ProviderSubscription(
            provider_subscription_id=f"sub_{len(self.subscribes)}",
            status="active",
            current_period_started_at=CYCLE_STARTED_AT,
            current_period_ended_at=CYCLE_ENDED_AT,
            plan=plan,
        )

    def set_subscription_plan(
        self, *, provider_subscription_id: str, plan: BillingPlanId
    ) -> ProviderSubscription:
        raise AssertionError("provisioning must not change anyone's plan")

    def subscription(self, *, provider_subscription_id: str) -> ProviderSubscription:
        raise AssertionError("provisioning already holds what the provider returned")

    def create_credit_grant(
        self,
        *,
        account_id: str,
        provider_customer_id: str,
        amount_nanos: int,
        period_started_at: datetime,
        period_ended_at: datetime,
    ) -> ProviderCreditGrant:
        del provider_customer_id, amount_nanos, period_started_at
        self.grants.append(account_id)
        return ProviderCreditGrant(
            provider_credit_grant_id=f"credgr_{len(self.grants)}",
            amount_nanos=FREE_PLAN_INCLUDED_NANOS,
            expires_at=period_ended_at,
        )

    def expire_credit_grant(self, *, provider_credit_grant_id: str) -> None:
        raise AssertionError("provisioning must not expire an allowance")

    def invoice_metered_totals(self, *, provider_invoice_id: str) -> Mapping[str, int]:
        raise AssertionError("registering must not read invoices")


@contextmanager
def _postgres_database() -> Iterator[DatabaseClient]:
    base_url_value = os.getenv("LAZYCLOUD_TEST_DATABASE_URL")
    if base_url_value is None:
        pytest.skip("LAZYCLOUD_TEST_DATABASE_URL is not configured")
    base_url = make_url(base_url_value)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("LAZYCLOUD_TEST_DATABASE_URL is not PostgreSQL")
    database_name = f"billing_invariants_{uuid4().hex}"
    admin = DatabaseClient.from_settings(
        DatabaseSettings(
            url=base_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    database: DatabaseClient | None = None
    try:
        with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        database = DatabaseClient.from_settings(
            DatabaseSettings(
                url=base_url.set(database=database_name).render_as_string(hide_password=False),
                pool_size=6,
                max_overflow=0,
                application_name=DatabaseApplicationName.Test,
            )
        )
        database.create_schema()
        yield database
    finally:
        if database is not None:
            database.dispose()
        with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()


def test_postgresql_refuses_two_rate_rows_in_force_at_the_same_instant() -> None:
    """Overlapping validity windows would make "the rate at t" a row-order answer.

    Two rows covering one instant is a wrong charge with no error behind it, so
    the exclusion is on the table rather than in the publisher that happens to
    close its predecessor.
    """

    at = datetime(2027, 1, 1, tzinfo=UTC)
    with _postgres_database() as database:
        with database.session() as session:
            session.add(
                ComputeRateTable(
                    id=str(uuid4()),
                    billing_owner=UsageBillingOwner.PlatformFleet.value,
                    gpu_type="",
                    pricing_version="test.a",
                    effective_at=at,
                    valid_until=at + timedelta(days=30),
                    nanos_per_container_second=Decimal(1),
                    nanos_per_cpu_core_second=Decimal(0),
                    nanos_per_memory_gib_second=Decimal(0),
                    nanos_per_gpu_card_second=Decimal(0),
                )
            )
            session.add(
                PlatformRateTable(
                    id=str(uuid4()),
                    pricing_version="test.a",
                    effective_at=at,
                    valid_until=at + timedelta(days=30),
                    nanos_per_egress_byte=Decimal(0),
                    nanos_per_volume_byte_second=Decimal(0),
                )
            )

        with (
            pytest.raises(IntegrityError, match="ex_billing_compute_rates_window"),
            database.session() as session,
        ):
            session.add(
                ComputeRateTable(
                    id=str(uuid4()),
                    billing_owner=UsageBillingOwner.PlatformFleet.value,
                    gpu_type="",
                    pricing_version="test.b",
                    effective_at=at + timedelta(days=1),
                    valid_until=None,
                    nanos_per_container_second=Decimal(2),
                    nanos_per_cpu_core_second=Decimal(0),
                    nanos_per_memory_gib_second=Decimal(0),
                    nanos_per_gpu_card_second=Decimal(0),
                )
            )

        with (
            pytest.raises(IntegrityError, match="ex_billing_platform_rates_window"),
            database.session() as session,
        ):
            session.add(
                PlatformRateTable(
                    id=str(uuid4()),
                    pricing_version="test.b",
                    effective_at=at + timedelta(days=1),
                    valid_until=None,
                    nanos_per_egress_byte=Decimal(1),
                    nanos_per_volume_byte_second=Decimal(0),
                )
            )


def _await_lock_wait(database: DatabaseClient, registration: Future[BillingAccount]) -> None:
    """Hold until a second registration is waiting on the first, or say why it is not.

    Both signals are read every cycle, and the second is the defect this test
    exists to catch: a registration that reached the end without ever waiting has
    already been to the provider on its own, which is the second customer.
    """

    for _ in range(200):
        with database.session() as session:
            blocked = session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        if blocked:
            return
        if registration.done():
            raise AssertionError("the second registration finished without waiting on the first")
        sleep(0.025)
    raise AssertionError("the second registration never reached anything it had to wait on")


def test_postgresql_two_first_sign_ins_provision_one_account_and_refuse_nobody() -> None:
    """Nobody is turned away from signing in for having raced themselves.

    Provisioning happens on the sign-in path, so two of them for one account
    arrive together whenever somebody double-clicks, retries, or has two tabs
    open. Both would otherwise read no row and both would go to the provider: two
    customers, of which the unique constraint refuses one — a person unable to
    sign in — and two subscriptions, which is worse, because both carry the three
    metered prices and the account's usage would be counted onto two invoices
    with only one of them named here.

    Three facts, and each is the point: exactly one customer, exactly one
    subscription and exactly one grant exist at the provider, and both callers
    came back with them.
    """

    with _postgres_database() as database:
        with database.session() as session:
            user_id = UserRepository(session).create(display_name="registration-race").id
            workspace_id = WorkspaceRepository(session).create(name=f"register-{uuid4()}").id
        provider = _RegistrationCountingProvider()

        def register() -> BillingAccount:
            with database.session() as session:
                account = BillingAccountService(session).billing_account_for(
                    provider,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                session.commit()
                return account

        with ThreadPoolExecutor(max_workers=1) as executor:
            with database.session() as first_session:
                first = BillingAccountService(first_session).billing_account_for(
                    provider,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                second = executor.submit(register)
                # The row above is inserted and locked but uncommitted. Held here
                # until the second call is provably waiting on it, because a
                # commit before it got that far would let it read the finished
                # provisioning and prove nothing about racing one.
                _await_lock_wait(database, second)
            second_account = second.result(timeout=10)

        with database.session() as session:
            stored = BillingAccountRepository(session).get_by_user(user_id)

    assert provider.registrations == [user_id]
    # Named by customer rather than by account, because that is what the provider
    # is asked to subscribe. One entry either way is the count that matters.
    assert provider.subscribes == [first.provider_customer_id]
    assert provider.grants == [user_id]
    assert stored is not None
    assert first.provider_customer_id == second_account.provider_customer_id
    assert first.provider_subscription_id == second_account.provider_subscription_id
    assert stored.provider_customer_id == first.provider_customer_id
    assert stored.provider_subscription_id == first.provider_subscription_id
    assert stored.plan is BillingPlanId.Free


def test_postgresql_one_meter_event_is_claimed_and_settled_by_one_drainer() -> None:
    """Two drainers never hold the same row, and neither settles the other's.

    A meter event sent twice outside the provider's deduplication window is a
    second charge, so the claim has to be exclusive; and an acknowledgement
    written under a claim somebody else now holds would mark a row sent that is
    still in flight.
    """

    now = utc_now()
    with _postgres_database() as database:
        with database.session() as session:
            workspace_id = WorkspaceRepository(session).create(name=f"outbox-{uuid4()}").id
            for index in range(2):
                session.add(
                    BillingMeterOutboxTable(
                        id=str(uuid4()),
                        workspace_id=workspace_id,
                        identifier=f"{workspace_id}-{index}",
                        provider_customer_id="cus_test",
                        meter_event_name="lazycloud_compute_runtime",
                        value_nanos=1_000,
                        pricing_version="test.a",
                        occurred_at=now,
                        status="pending",
                        attempts=0,
                        next_attempt_at=now,
                    )
                )

        second_claim: list[str] = []

        def claim_second() -> None:
            with database.session() as session:
                claimed = BillingMeterOutboxRepository(session).claim(
                    now=now,
                    limit=2,
                    claim_token="22222222-2222-4222-8222-222222222222",
                )
                second_claim.extend(event.id for event in claimed)

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            database.session() as first_session,
        ):
            first_claim = BillingMeterOutboxRepository(first_session).claim(
                now=now,
                limit=1,
                claim_token="11111111-1111-4111-8111-111111111111",
            )
            second = executor.submit(claim_second)
            second.result(timeout=10)

        assert len(first_claim) == 1
        assert len(second_claim) == 1
        assert first_claim[0].id not in second_claim

        with database.session() as session:
            outbox = BillingMeterOutboxRepository(session)
            stolen = outbox.mark_sent(
                event_id=second_claim[0],
                claim_token="11111111-1111-4111-8111-111111111111",
                now=now,
            )
            settled = outbox.mark_sent(
                event_id=second_claim[0],
                claim_token="22222222-2222-4222-8222-222222222222",
                now=now,
            )
        assert (stolen, settled) == (False, True)
