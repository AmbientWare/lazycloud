from __future__ import annotations

from dataclasses import dataclass

from database.repositories.billing import BillingAccountRepository
from database.repositories.identity import WorkspaceMemberRepository
from shared.billing_accounts import BillingAccountStatus
from shared.errors import PaymentRequiredError
from sqlalchemy.orm import Session


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

    One question, asked once of every account: will what this runs reach an
    invoice somebody is paying? How much it costs is not part of it. Every
    account holds a subscription carrying the metered prices, so overage is
    billed by the provider and chased through their card — spending past what a
    plan includes is something to invoice, never something to refuse on, and
    there is no second shape here for the accounts that have not paid yet.

    Two things make the answer no, and neither is an amount. The provider has
    reported that a payment did not go through; or the account holds no
    subscription for its usage to be billed on, which is an account that has
    never been provisioned, one whose provisioning stopped part-way, and one
    whose subscription the provider says has ended. Work started by an account in
    the second state is metered into a ledger, sent to a meter, and lands on no
    invoice at all — unbounded compute nobody is charged for. Provisioning
    happens at sign-in and at the first billing surface an account reaches, so
    this is a state nothing should be in; a refusal here is what makes that a
    fact rather than an expectation.

    Read from the local row rather than from the provider, because this runs on
    every container start and a network round trip there is a start that fails
    whenever the provider is slow.
    """

    def assert_solvent(self, session: Session, *, workspace_id: str) -> None:
        owner = WorkspaceMemberRepository(session).owner(workspace_id)
        if owner is None:
            # A workspace with no owner row is reachable by nobody, so there is
            # no account to judge and nothing this can decide.
            return None
        account = BillingAccountRepository(session).get_by_user(owner.user_id)
        if account is None or not account.provider_subscription_id or account.plan is None:
            raise PaymentRequiredError(
                "this account holds no subscription for its usage to be billed on; "
                "sign in again to finish setting it up"
            )
        if account.status is BillingAccountStatus.PastDue:
            raise PaymentRequiredError(
                "a payment for this account did not go through; "
                "update the card on file to start new work"
            )


__all__ = ["DatabaseBillingAdmission"]
