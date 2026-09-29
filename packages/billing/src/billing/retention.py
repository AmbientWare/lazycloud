from __future__ import annotations

from database.repositories.billing import BillingAccountRepository
from database.types import DatabaseSession
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import complimentary_terms, published_plan


def workspace_retention_days(session: DatabaseSession, workspace_id: str) -> int:
    plan, complimentary = BillingAccountRepository(session).workspace_retention_terms(workspace_id)
    if complimentary:
        return complimentary_terms().entitlements.retention_days
    return published_plan(plan or BillingPlanId.Free).entitlements.retention_days
