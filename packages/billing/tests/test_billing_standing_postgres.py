from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from billing.standing import reconcile_standing
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient
from database.client import DatabaseClient
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_periods import BillingPeriodRepository
from database.settings import DatabaseApplicationName, DatabaseSettings
from shared.billing_accounts import BillingAccountStatus, BillingPlan
from shared.billing_periods import BillingPeriodStatus
from sqlalchemy import text
from sqlalchemy.engine import make_url
from storage.volume_filesystem import LocalVolumeFilesystem
from tests.redis_fakes import FakeRedis
from tests.service_fixtures import owned_workspace, workspace_owner_user_id

_SEPTEMBER = date(2026, 9, 1)
_OCTOBER = date(2026, 10, 1)


def test_two_payment_outcomes_at_once_cannot_leave_a_failed_month_in_good_standing(
    tmp_path: Path,
) -> None:
    """Standing is derived, and two things derive it concurrently now.

    The charge at close learns an outcome, and so does the webhook — in separate
    transactions, on separate connections. Under `READ COMMITTED` each reads what
    the other has not committed yet, so without a row lock both can conclude
    there is nothing to change: the one that settles a failure sees the other's
    uncommitted `Active` and agrees, and the account ends up in good standing
    owing money.

    Only a real PostgreSQL proves this. SQLite serializes writers, so the
    interleaving the lock exists for cannot occur there at all.
    """

    with _postgres_services(tmp_path) as services:
        with services.context.database.session() as session:
            workspace_id = services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(services.context, workspace_id)
        _account(services, user_id, BillingAccountStatus.PastDue)
        _invoiced(services, user_id, _SEPTEMBER, BillingPeriodStatus.PaymentFailed)
        _invoiced(services, user_id, _OCTOBER, BillingPeriodStatus.Invoiced)

        # September holds its transaction open until October has had its turn, so
        # the interleaving the lock exists for is forced rather than hoped for.
        # October blocks on the lock when there is one, so September gives up
        # waiting after a moment and commits — that release is what lets October
        # through, and without a lock October never waited at all.
        september_reconciled = Event()
        october_reconciled = Event()

        def settle_september_paid() -> None:
            with services.context.database.session() as session:
                BillingPeriodRepository(session).settle(
                    user_id=user_id,
                    period_start=_SEPTEMBER,
                    status=BillingPeriodStatus.Paid,
                )
                reconcile_standing(session, user_id=user_id)
                september_reconciled.set()
                october_reconciled.wait(timeout=2)
                session.commit()

        def settle_october_failed() -> None:
            assert september_reconciled.wait(timeout=10)
            with services.context.database.session() as session:
                BillingPeriodRepository(session).settle(
                    user_id=user_id,
                    period_start=_OCTOBER,
                    status=BillingPeriodStatus.PaymentFailed,
                )
                reconcile_standing(session, user_id=user_id)
                october_reconciled.set()
                session.commit()

        with ThreadPoolExecutor(max_workers=2) as executor:
            paid = executor.submit(settle_september_paid)
            failed = executor.submit(settle_october_failed)
            paid.result(timeout=20)
            failed.result(timeout=20)

        with services.context.database.session() as session:
            account = BillingAccountRepository(session).get_by_user(user_id)
            outstanding = BillingPeriodRepository(session).outstanding_for(user_id=user_id)

        assert account is not None
        # October failed, so the account is behind however the two interleaved.
        assert account.status is BillingAccountStatus.PastDue
        assert [period.period_start for period in outstanding] == [_OCTOBER]


def _account(services: ApiServices, user_id: str, status: BillingAccountStatus) -> None:
    with services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            plan=BillingPlan.Free,
            status=status,
            provider_customer_id="cus_standing",
        )
        session.commit()


def _invoiced(
    services: ApiServices, user_id: str, period_start: date, status: BillingPeriodStatus
) -> None:
    """Drive a period to an issued state through the repository that owns it."""

    with services.context.database.session() as session:
        periods = BillingPeriodRepository(session)
        end = date(period_start.year + (period_start.month == 12), (period_start.month % 12) + 1, 1)
        periods.open_period(
            user_id=user_id,
            period_start=period_start,
            period_end=end,
            plan=BillingPlan.Free,
            currency="USD",
        )
        periods.close_period(
            user_id=user_id,
            period_start=period_start,
            plan=BillingPlan.Free,
            usage_cost_nanos=5_000_000_000,
            included_cost_nanos=0,
            subscription_cost_nanos=0,
            charged_cost_nanos=5_000_000_000,
        )
        periods.record_invoice(
            user_id=user_id,
            period_start=period_start,
            provider_invoice_id=f"in_{period_start.isoformat()}",
        )
        periods.mark_invoiced(
            user_id=user_id,
            period_start=period_start,
            provider_invoice_id=f"in_{period_start.isoformat()}",
        )
        if status is not BillingPeriodStatus.Invoiced:
            periods.settle(user_id=user_id, period_start=period_start, status=status)
        session.commit()


@contextmanager
def _postgres_services(tmp_path: Path) -> Iterator[ApiServices]:
    base_url_value = os.getenv("LAZYCLOUD_TEST_DATABASE_URL")
    if base_url_value is None:
        pytest.skip("LAZYCLOUD_TEST_DATABASE_URL is not configured")
    base_url = make_url(base_url_value)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("LAZYCLOUD_TEST_DATABASE_URL is not PostgreSQL")
    database_name = f"billing_standing_{uuid4().hex}"
    admin = DatabaseClient.from_settings(
        DatabaseSettings(
            url=base_url.render_as_string(hide_password=False),
            application_name=DatabaseApplicationName.Test,
        )
    )
    services: ApiServices | None = None
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
        redis = RedisClient(FakeRedis(), key_prefix=f"billing-standing-{uuid4()}")
        services = ApiServices.create(
            database,
            root=tmp_path,
            redis_client=redis,
            binary_redis_client=redis.with_key_prefix("binary"),
            owns_redis_client=False,
            owns_binary_redis_client=False,
            volume_filesystem=LocalVolumeFilesystem(tmp_path / "volumes"),
        )
        owned_workspace(ControlPlaneService(services.context), "default")
        yield services
    finally:
        if services is not None:
            services.close()
        with admin.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()
