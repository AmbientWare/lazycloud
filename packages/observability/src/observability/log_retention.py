from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic

from database.repositories.billing import BillingAccountRepository
from database.repositories.execution import LogRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.types import DatabaseSession
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import PUBLISHED_PLANS, published_plan
from shared.timestamps import utc_now

from observability.context import ObservabilityContext


@dataclass(frozen=True, slots=True)
class LogRetentionResult:
    deleted: int
    batches: int
    budget_exhausted: bool


@dataclass(slots=True)
class LogRetentionService:
    context: ObservabilityContext = field(repr=False)

    def cutoff(self, workspace_id: str, *, now: datetime | None = None) -> datetime:
        with self.context.database.session() as session:
            return self.cutoff_in_session(session, workspace_id, now=now)

    @staticmethod
    def cutoff_in_session(
        session: DatabaseSession, workspace_id: str, *, now: datetime | None = None
    ) -> datetime:
        owner = WorkspaceMemberRepository(session).owner(workspace_id)
        account = BillingAccountRepository(session).get_by_user(owner.user_id) if owner else None
        plan = BillingPlanId.Free
        if account is not None:
            if account.complimentary_since is not None:
                plan = BillingPlanId.Team
            elif account.plan is not None:
                plan = account.plan
        return (now or utc_now()) - timedelta(
            days=published_plan(plan).entitlements.log_retention_days
        )

    def prune(self, *, now: datetime | None = None, limit: int = 1_000) -> int:
        current = now or utc_now()
        cutoffs = {
            plan.id.value: current - timedelta(days=plan.entitlements.log_retention_days)
            for plan in PUBLISHED_PLANS
        }
        with self.context.database.session() as session:
            return LogRepository(session).prune(
                cutoffs=cutoffs,
                default_cutoff=cutoffs[BillingPlanId.Free.value],
                complimentary_cutoff=cutoffs[BillingPlanId.Team.value],
                limit=limit,
            )

    def run(self) -> LogRetentionResult:
        started = monotonic()
        deleted = 0
        for batch in range(100):
            count = self.prune()
            deleted += count
            if count < 1_000:
                return LogRetentionResult(deleted, batch + 1, False)
            if monotonic() - started >= 240:
                return LogRetentionResult(deleted, batch + 1, True)
        return LogRetentionResult(deleted, 100, True)
