from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.identity import WorkspaceMemberRepository
from observability.billing import UsageBillingOverview
from shared.billing_ledger import BillingLedgerEntry
from shared.errors import NotFoundError
from sqlalchemy.orm import Session


def utc_day_bounds(day: date) -> tuple[datetime, datetime]:
    """The half-open interval a day's usage is read over.

    Half-open so a window landing exactly on midnight belongs to the day that is
    starting, and is counted once across the pair rather than in both or neither.
    """

    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


@dataclass(frozen=True, slots=True)
class BillingLedgerService:
    """Turns a day of metered usage into the priced record an invoice is built from."""

    session: Session

    def record_day(self, overview: UsageBillingOverview) -> int:
        """Write what a workspace owed on the UTC day this overview covers.

        The overview carries the workspace and the interval, so neither is a
        separate argument: the same report priced for a month is a perfectly
        valid overview, and a signature that accepted it alongside a day would
        write a month of one workspace's cost as one day of another's, silently
        and permanently.

        The report itself is passed in rather than fetched here because it is the
        one the dashboard renders. Pricing again from the same rollup would be
        two implementations of one answer, disagreeing the first time either
        moved.
        """

        day = overview.start.astimezone(UTC).date()
        user_id = self._payer_for(overview.workspace_id)
        if (overview.start, overview.end) != utc_day_bounds(day):
            raise ValueError(
                "a ledger day is one whole UTC day; "
                f"got {overview.start.isoformat()} to {overview.end.isoformat()}"
            )
        entries = tuple(
            BillingLedgerEntry(
                workspace_id=overview.workspace_id,
                user_id=user_id,
                day=day,
                metric=line.metric,
                variant=line.variant,
                effective_date=line.effective_date,
                quantity=line.quantity,
                unit=line.unit,
                price_per_unit_nanos=line.price_per_unit_nanos,
                cost_nanos=line.cost_nanos,
                currency=overview.currency,
            )
            for line in overview.summary
        )
        return BillingLedgerRepository(self.session).record_day(
            workspace_id=overview.workspace_id, day=day, entries=entries
        )

    def _payer_for(self, workspace_id: str) -> str:
        """Who owes for this workspace's usage.

        Resolved now, while the workspace still exists, and written onto every
        line: a debt whose payer had to be looked up at invoice time would vanish
        with the workspace.

        Once it is gone its membership is gone too, and the last part-day it ran
        is priced after the fact. The ledger is asked then, because it already
        holds the answer from the days that were priced while the workspace was
        alive — and a workspace deleted the same day it was created leaves
        nothing to bill and nobody to bill it to, which is a failure and not a
        zero.
        """

        owner = WorkspaceMemberRepository(self.session).owner(workspace_id)
        if owner is not None:
            return owner.user_id
        recorded = BillingLedgerRepository(self.session).last_payer_for(workspace_id)
        if recorded is None:
            raise NotFoundError(f"workspace has no owner and no billed history: {workspace_id}")
        return recorded


__all__ = ["BillingLedgerService", "utc_day_bounds"]
