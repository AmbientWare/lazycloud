from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic

from billing.retention import workspace_retention_days
from database.repositories.execution import LogRepository
from database.types import DatabaseSession
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import PUBLISHED_PLANS, complimentary_terms
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
        return (now or utc_now()) - timedelta(days=workspace_retention_days(session, workspace_id))

    def prune(self, *, now: datetime | None = None, limit: int = 1_000) -> int:
        current = now or utc_now()
        cutoffs = {
            plan.id.value: current - timedelta(days=plan.entitlements.retention_days)
            for plan in PUBLISHED_PLANS
        }
        with self.context.database.session() as session:
            return LogRepository(session).prune(
                cutoffs=cutoffs,
                default_cutoff=cutoffs[BillingPlanId.Free.value],
                complimentary_cutoff=current
                - timedelta(days=complimentary_terms().entitlements.retention_days),
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
