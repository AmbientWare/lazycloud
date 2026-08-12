from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from database.client import DatabaseClient
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.identity import WorkspaceMemberRepository
from shared.billing_accounts import BillingAccount, BillingAccountStatus, BillingPlan
from shared.billing_plans import DEFAULT_BILLING_PLANS
from shared.errors import PaymentRequiredError
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from billing.periods import month_bounds


@dataclass(frozen=True, slots=True)
class DatabaseBillingAdmission:
    """Whether an account may start more work.

    Asked before a container exists, in the transaction that would create it, so
    a refusal leaves nothing behind — no row, no published change, and nothing
    reserved at a provider. This mirrors how a paused app is refused, and for the
    same reason: a check after the record is written has to undo it, and the
    version of that which runs after a crash never happens at all.

    Only ever refuses *new* work. Containers already running keep running and
    keep being metered, because stopping them would destroy work someone is in
    the middle of over a bill they have not been given the chance to pay.
    """

    def assert_solvent(self, session: Session, *, workspace_id: str) -> None:
        account = BillingAccountRepository(session).get_for_workspace_owner(workspace_id)
        if account is None:
            # No payment relationship at all. The free allowance still applies,
            # and the ledger records what was spent against the owner, so this
            # falls through to the allowance check with the free plan's numbers.
            return self._assert_within_allowance(session, workspace_id=workspace_id, account=None)
        if account.status is BillingAccountStatus.PastDue:
            raise PaymentRequiredError(
                "a payment for this account did not go through; "
                "update the card on file to start new work"
            )
        if account.provider_customer_id:
            # There is a way to charge them. Running past the allowance is then
            # what a paid plan is for, not a reason to stop: the overage is
            # billed at the end of the month like everything else.
            return None
        return self._assert_within_allowance(session, workspace_id=workspace_id, account=account)

    def _assert_within_allowance(
        self, session: Session, *, workspace_id: str, account: BillingAccount | None
    ) -> None:
        """Refuse an account with no way to pay once its free usage is spent.

        Nothing here can be collected — there is no card — so the allowance is
        the whole of what this account may run. Left ungated it would run up a
        debt that the close turns into an invoice nobody can charge.
        """

        # Absence is the free plan, which is the whole reason no row is written
        # to record that somebody has not agreed to pay.
        # The free plan's allowance whatever plan the row names. An account with
        # no way to be charged has been given exactly the credit this platform
        # extends to someone who has not agreed to pay — a paid plan's larger
        # allowance is part of what its subscription buys, and there is nothing
        # here to collect that subscription from.
        included = DEFAULT_BILLING_PLANS.for_plan(BillingPlan.Free).included_cost_nanos
        payer = account.user_id if account is not None else _owner_of(session, workspace_id)
        if payer is None:
            # Nobody owns this workspace, so nobody can be billed for it and
            # nothing here can be priced either. Refused rather than waved
            # through: the alternative is compute that is free by accident.
            raise PaymentRequiredError(f"workspace {workspace_id} has no billable owner")
        accrued = BillingLedgerRepository(session).accrued_since(
            user_id=payer, since=_period_start()
        )
        if accrued >= included:
            raise PaymentRequiredError(
                "this month's included usage is spent; add a payment method to start new work"
            )
        return None


def _owner_of(session: Session, workspace_id: str) -> str | None:
    owner = WorkspaceMemberRepository(session).owner(workspace_id)
    return owner.user_id if owner is not None else None


def _period_start() -> date:
    start, _ = month_bounds(utc_now().date())
    return start


@dataclass(frozen=True, slots=True)
class ImageBuildBillingAdmission:
    """The same decision, for a build that opens its own session.

    Builds reach the worker without a container record and so without the
    transaction the container gate rides in. They still have to ask, because the
    compute is billed the same way — so this owns the session rather than taking
    one.
    """

    database: DatabaseClient

    def assert_solvent(self, *, workspace_id: str) -> None:
        with self.database.session() as session:
            DatabaseBillingAdmission().assert_solvent(session, workspace_id=workspace_id)


__all__ = ["DatabaseBillingAdmission", "ImageBuildBillingAdmission"]
