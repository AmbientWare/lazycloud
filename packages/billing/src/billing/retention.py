from __future__ import annotations

from database.repositories.billing import BillingAccountRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.types import DatabaseSession
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import complimentary_terms, published_plan


def workspace_retention_days(session: DatabaseSession, workspace_id: str) -> int:
    owner = WorkspaceMemberRepository(session).owner(workspace_id)
    account = BillingAccountRepository(session).get_by_user(owner.user_id) if owner else None
    plan = BillingPlanId.Free
    if account is not None:
        if account.complimentary_since is not None:
            return complimentary_terms().entitlements.retention_days
        elif account.plan is not None:
            plan = account.plan
    return published_plan(plan).entitlements.retention_days
