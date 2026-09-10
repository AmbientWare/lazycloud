from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from control.service import ControlPlaneService
from database.context import ServiceContext
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.identity import (
    UserRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from pydantic import JsonValue
from shared.billing_accounts import BillingAccountStatus
from shared.billing_credits import CreditGrant, CreditKind
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_rate_card import (
    ONE_TIME_TRIAL_NANOS,
    TRIAL_VALIDITY_DAYS,
)
from shared.identity import (
    WorkspaceRecord,
    WorkspaceStorageConfig,
)
from shared.timestamps import utc_now
from sqlalchemy import Engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings
from tests.database_fixtures import temporary_database


def _fixture_account(database: DatabaseClient, display_name: str) -> str:
    """A Free signup with its local trial; provider identifiers are fixture-owned."""

    with database.session() as session:
        user_id = UserRepository(session).create(display_name=display_name).id
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_fixture_{user_id}",
            provider_subscription_id=f"sub_fixture_{user_id}",
            plan=BillingPlanId.Free,
            subscription_terms_version=SubscriptionTermsVersion.Free,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        now = utc_now()
        allowance = BillingAllowanceRepository(session)
        allowance.set_subscription_period(
            user_id=user_id,
            period_started_at=now,
            period_ended_at=now + timedelta(days=30),
            allowance_nanos=0,
        )
        allowance.record_funded_terms(
            user_id=user_id, period_started_at=now, terms_version=SubscriptionTermsVersion.Free
        )
        allowance.confirm_credit(user_id=user_id, period_started_at=now, at=now)
        credits = BillingCreditRepository(session)
        credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                source_id=f"trial:{user_id}",
                kind=CreditKind.Trial,
                amount_nanos=ONE_TIME_TRIAL_NANOS,
                effective_at=now,
                expires_at=now + timedelta(days=TRIAL_VALIDITY_DAYS),
            ),
        )
        return user_id


def owned_workspace(
    control: ControlPlaneService,
    name: str = "default",
    *,
    storage: WorkspaceStorageConfig | None = None,
    labels: dict[str, str] | None = None,
    metadata: Mapping[str, JsonValue] | None = None,
) -> WorkspaceRecord:
    """A workspace with the owner row production writes in the same transaction.

    Tests that only need a workspace to exist go through here rather than writing the
    row alone: everything a workspace resolves through its account—compute, domains,
    credentials—needs that row, and a workspace without one exists nowhere else.
    """
    owner_user_id = _existing_owner(control, name) or _fixture_account(
        control.context.database,
        f"{name}-owner-{uuid4().hex[:8]}",
    )
    return control.set_workspace(
        name,
        owner_user_id=owner_user_id,
        storage=storage,
        labels=labels,
        metadata=metadata,
    )


def _existing_owner(control: ControlPlaneService, name: str) -> str | None:
    with control.context.database.session() as session:
        record = WorkspaceRepository(session).by_name(name)
        if record is None:
            return None
        owner = WorkspaceMemberRepository(session).owner(record.id)
    return owner.user_id if owner is not None else None


def unbilled_account(context: ServiceContext) -> tuple[str, str]:
    """A workspace and its owner, with nothing billing has ever written.

    The opposite of what `_fixture_account` leaves, and the state the billing
    tests need: what provisioning does on first reaching an account cannot be
    observed against one a fixture has already stood a row up for.
    """

    with context.database.session() as session:
        user_id = UserRepository(session).create(display_name="unprovisioned").id
        workspace_id = WorkspaceRepository(session).create(name=f"unbilled-{uuid4()}").id
        WorkspaceMemberRepository(session).ensure_owner(workspace_id=workspace_id, user_id=user_id)
    return user_id, workspace_id


def unfunded_billing_account(
    context: ServiceContext, *, period_started_at: datetime, period_ended_at: datetime
) -> tuple[str, str]:
    """A subscribed account without local credit grants."""
    user_id, workspace_id = unbilled_account(context)
    with context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_fixture_{user_id}",
            provider_subscription_id=f"sub_fixture_{user_id}",
            plan=BillingPlanId.Free,
            subscription_terms_version=SubscriptionTermsVersion.FreeLegacy,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=user_id,
            period_started_at=period_started_at,
            period_ended_at=period_ended_at,
            allowance_nanos=0,
        )
    return user_id, workspace_id


def carded_account(context: ServiceContext) -> tuple[str, str]:
    """An unprovisioned workspace and owner whose account already holds a card.

    What a plan's own terms can only be observed against. An account with no card
    is given what the platform will spend to find out whether it can bill anybody,
    whatever plan it is on — so a test that put an account on Team and read back
    the plan's allowance would be reading the cardless figure and calling it a
    plan.

    The row is written before provisioning rather than after, because
    provisioning is what buys the first grant and a card attached afterwards
    would be a cycle already funded at the wrong figure.
    """

    user_id, workspace_id = unbilled_account(context)
    with context.database.session() as session:
        accounts = BillingAccountRepository(session)
        accounts.lock_for_registration(user_id)
        accounts.set_payment_method_present(user_id=user_id, present=True, at=utc_now())
        session.commit()
    return user_id, workspace_id


def workspace_owner_user_id(context: ServiceContext, workspace_id: str) -> str:
    """The account that owns a workspace, created on first ask.

    Production writes the owner row with the workspace, so anything resolving compute
    or domains through the account finds one. Tests that build a workspace through a
    lower-level path need the same row before they can join a machine to it.
    """
    with context.database.session() as session:
        existing = WorkspaceMemberRepository(session).owner(workspace_id)
    if existing is not None:
        return existing.user_id
    user_id = _fixture_account(context.database, f"owner-{uuid4().hex[:12]}")
    with context.database.session() as session:
        WorkspaceMemberRepository(session).ensure_owner(
            workspace_id=workspace_id,
            user_id=user_id,
        )
    return user_id


@pytest.fixture(scope="session")
def workspace_template_url(
    postgres_admin: Engine, migrated_template_url: URL, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[URL]:
    with temporary_database(postgres_admin, template=migrated_template_url) as url:
        database = DatabaseClient.from_settings(
            DatabaseSettings(
                url=url.render_as_string(hide_password=False),
                application_name=DatabaseApplicationName.Test,
            )
        )
        try:
            context = ServiceContext.create(
                database, root=tmp_path_factory.mktemp("domain"), create_schema=False
            )
            owned_workspace(ControlPlaneService(context), "default")
        finally:
            database.dispose()
        yield url


@pytest.fixture(scope="session")
def domain_database(postgres_admin: Engine, seeded_template_url: URL) -> Iterator[DatabaseClient]:
    with temporary_database(postgres_admin, template=seeded_template_url) as url:
        database = DatabaseClient.from_settings(
            DatabaseSettings(
                url=url.render_as_string(hide_password=False),
                application_name=DatabaseApplicationName.Test,
            )
        )
        try:
            yield database
        finally:
            database.dispose()


@pytest.fixture
def service_context(domain_database: DatabaseClient, tmp_path: Path) -> Iterator[ServiceContext]:
    # Only single-connection owner tests use this fixture. A commit releases a
    # savepoint; tests of cross-connection visibility keep their real commits.
    with domain_database.engine.connect() as connection, connection.begin() as transaction:
        database = DatabaseClient(
            settings=domain_database.settings,
            engine=domain_database.engine,
            sessions=sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            ),
        )
        try:
            yield ServiceContext.create(database, root=tmp_path, create_schema=False)
        finally:
            assert transaction.is_active, "owner test ended its outer isolation transaction"
            transaction.rollback()
