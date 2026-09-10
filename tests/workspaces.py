from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from uuid import uuid4

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
from identity.auth import TokenIssuer
from identity.users import UserService
from pydantic import JsonValue
from shared.billing_accounts import BillingAccountStatus
from shared.billing_credits import CreditGrant, CreditKind
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_rate_card import (
    ONE_TIME_TRIAL_NANOS,
    TRIAL_VALIDITY_DAYS,
)
from shared.identity import (
    AuthTokenRecord,
    PlatformRole,
    TokenKind,
    WorkspaceRecord,
    WorkspaceStorageConfig,
)
from shared.timestamps import utc_now

from database import DatabaseClient


def _fixture_account(database: DatabaseClient, display_name: str) -> str:
    """Create a Free account with trial credit and local provider identifiers."""

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
    """Create or update a workspace with an owner account for billing and credentials."""
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
    """Create a workspace and owner without billing records to exercise first provisioning."""

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
    """Attach a card before provisioning so the first grant uses the selected plan's terms."""

    user_id, workspace_id = unbilled_account(context)
    with context.database.session() as session:
        accounts = BillingAccountRepository(session)
        accounts.lock_for_registration(user_id)
        accounts.set_payment_method_present(user_id=user_id, present=True, at=utc_now())
        session.commit()
    return user_id, workspace_id


def workspace_owner_user_id(context: ServiceContext, workspace_id: str) -> str:
    """Return the owner, creating one for workspaces inserted directly through repositories."""
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


def administrator_credential(
    context: ServiceContext,
    name: str = "administrator",
) -> tuple[str, AuthTokenRecord]:
    """Issue an administrator's account token without adding workspace memberships."""
    user = UserService(context).create(
        display_name=f"admin-{uuid4().hex[:12]}",
        role=PlatformRole.Administrator,
    )
    issuer = TokenIssuer(context)
    with context.database.session() as session:
        raw_token, record = issuer.issue_for_user(
            session,
            name,
            user_id=user.id,
            kind=TokenKind.Admin,
        )
    issuer.committed()
    return raw_token, record
