from __future__ import annotations

from dataclasses import dataclass

from database.repositories.billing import BillingAccountRepository
from database.repositories.identity import UserRepository
from shared.billing_accounts import (
    FREE_BILLING_ACCOUNT,
    BillingAccount,
    BillingAccountState,
    BillingAccountStatus,
    BillingPlan,
)
from shared.errors import NotFoundError
from shared.payments import PaymentProvider
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class BillingAccountService:
    """What a workspace is on, and who to charge for it."""

    session: Session

    def resolve_for_workspace(self, workspace_id: str) -> BillingAccountState:
        """What is in force for this workspace right now.

        Absence is the free plan rather than an error. Nothing writes a row to
        record that an account has not agreed to pay, so every workspace has an
        answer here from the moment it exists, and no caller has to decide what a
        missing row means.
        """

        account = BillingAccountRepository(self.session).get_for_workspace_owner(workspace_id)
        return account.state if account is not None else FREE_BILLING_ACCOUNT

    def payment_account_for(self, *, user_id: str) -> BillingAccount:
        """The account of someone who has already registered to pay.

        Refuses rather than registering. The caller wanting this has something to
        show a person about a payment relationship that exists — past invoices, a
        card to replace — and creating one on the way there would open a page
        with nothing on it and leave a provider customer behind for someone who
        never meant to make one.
        """

        account = BillingAccountRepository(self.session).get_by_user(user_id)
        if account is None or not account.provider_customer_id:
            raise NotFoundError(f"no payment relationship to manage for user: {user_id}")
        return account

    def payment_customer_for(
        self, payments: PaymentProvider, *, user_id: str, workspace_id: str
    ) -> BillingAccount:
        """The account to bill this person through, registering them if needed.

        The first production writer of a `billing_accounts` row, and the answer to
        where one comes from: a customer has to exist at the provider before
        anyone can be shown a page to save a card on, so the row is created at
        the moment somebody sets out to pay rather than at sign-up. An account
        per person who ever registered would be a provider customer per signup,
        almost all of whom never pay.

        The plan is not touched. Saving a card is not choosing a plan, and an
        account that had already chosen one must not be moved back by coming here
        to replace an expired card.

        Idempotent by the row, not by the provider: an account that already names
        a customer returns it rather than registering a second, which would leave
        two customers holding one person's invoices and only one of them known
        here.
        """

        accounts = BillingAccountRepository(self.session)
        # Locked, not merely read. Two card pages opened at once would otherwise
        # both find no customer and both register one; the row insert settles
        # which wins, but only after the loser has already created a customer at
        # the provider that nothing here will ever name again.
        existing = accounts.get_by_user(user_id, for_update=True)
        if existing is not None and existing.provider_customer_id:
            return existing
        user = UserRepository(self.session).get(user_id)
        if user is None:
            raise NotFoundError(f"no user to bill: {user_id}")
        customer = payments.create_customer(email=user.email, workspace_id=workspace_id)
        return accounts.upsert(
            user_id=user_id,
            plan=existing.plan if existing is not None else BillingPlan.Free,
            status=existing.status if existing is not None else BillingAccountStatus.Active,
            provider_customer_id=customer.provider_customer_id,
        )


__all__ = ["BillingAccountService"]
