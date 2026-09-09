from __future__ import annotations

from dataclasses import dataclass

from database.repositories.billing import BillingAccountRepository
from database.repositories.identity import UserRepository, WorkspaceMemberRepository
from shared.billing_accounts import BillingAccount
from shared.billing_plans import BillingPlanId
from shared.errors import NotFoundError, UpstreamUnavailableError
from shared.payments import PaymentProvider
from sqlalchemy.orm import Session

from billing.credits import initialize_local_credits
from billing.periods import carry_plan_into_cycle


@dataclass(frozen=True, slots=True)
class BillingAccountService:
    """Who pays for a workspace, and how they are reached at the provider."""

    session: Session

    def payment_account_for(self, *, user_id: str) -> BillingAccount:
        """The account of someone already registered at the payment provider.

        Refuses rather than registering. The caller wanting this has something to
        show a person about a payment relationship that exists — past invoices, a
        card to replace — and the only accounts that reach here without one never
        signed in, so there is nothing at the provider to show them. Registering
        on the way there would open a page with nothing on it.
        """

        account = BillingAccountRepository(self.session).get_by_user(user_id)
        if account is None or not account.provider_customer_id:
            raise NotFoundError(f"no payment relationship to manage for user: {user_id}")
        return account

    def billing_account_for(
        self, payments: PaymentProvider, *, user_id: str, workspace_id: str
    ) -> BillingAccount:
        """The account this person is billed through, provisioned if it is not.

        The writer of a `billing_accounts` row, and the answer to where one comes
        from: sign-in provisions before it mints a session, so an account that
        exists is an account that can be billed, and every later page that needs
        one — a card to save, a plan to change — finds it here rather than
        creating it.

        Provisioning is a customer at the provider, a subscription on the free
        plan carrying that plan's price and the three metered prices, the
        allowance period the subscription's own cycle defines, and the grant that
        funds it. One plan shape rather than two: the account that has never paid
        and the account that pays are the same objects with a different price, so
        overage is billed for both instead of refused for one.

        Idempotent, because every one of those can be repeated. An account whose
        row already names a subscription returns it and reaches no provider at
        all. An account part-way through — a customer registered and a
        subscription that was not — is finished here, and the calls it repeats
        are keyed on the account so the provider answers with what the first
        attempt made rather than a second of each.
        """

        accounts = BillingAccountRepository(self.session)
        # Read before anything is written or locked. Every sign-in after the
        # first is answered here, so the ordinary path takes no lock and holds no
        # transaction open across a provider it never calls.
        registered = accounts.get_by_user(user_id)
        if registered is not None and _provisioned(registered):
            return registered
        # Provisioning is serialized on the account's own row, which is created
        # first so that there is a row to serialize on. The lock is deliberately
        # held across the provider calls: it is what makes the second of two
        # simultaneous first sign-ins wait and then read what the first created,
        # instead of creating a second customer and a second subscription — a
        # person turned away for racing themselves, and their usage metered onto
        # two invoices.
        return self._provision(
            payments,
            accounts.lock_for_registration(user_id),
            user_id=user_id,
            workspace_id=workspace_id,
        )

    def _provision(
        self,
        payments: PaymentProvider,
        existing: BillingAccount,
        *,
        user_id: str,
        workspace_id: str,
    ) -> BillingAccount:
        """Finish this account's provisioning under the lock the caller took.

        Takes the locked row rather than reading it again, so registering a
        customer, subscribing them and granting what the plan includes are one
        decision made under one lock instead of several that can disagree.

        The plan written down is the one the provider's answer carries rather
        than the one asked for. A customer who already holds a live subscription
        is given it back instead of a second, and an account whose row lost track
        of a Team subscription must not be recorded as free — it would be shown
        terms cheaper than the ones it is being charged for.
        """

        if _provisioned(existing):
            return existing
        customer_id = self._customer_id(
            payments, existing, user_id=user_id, workspace_id=workspace_id
        )
        subscription = payments.create_subscription(
            provider_customer_id=customer_id,
            plan=BillingPlanId.Free,
        )
        plan = subscription.plan
        if plan is None:
            raise UpstreamUnavailableError(
                f"the payment provider holds a subscription for {user_id} on a price this "
                "platform did not publish, so there are no terms to open its cycle on"
            )
        initialize_local_credits(
            self.session,
            payments,
            user_id=user_id,
            provider_customer_id=customer_id,
            effective_at=subscription.current_period_started_at,
        )
        return BillingAccountRepository(self.session).upsert(
            user_id=user_id,
            status=existing.status,
            provider_customer_id=customer_id,
            provider_subscription_id=subscription.provider_subscription_id,
            provider_credit_grant_id=carry_plan_into_cycle(
                self.session,
                payments,
                account_id=user_id,
                provider_customer_id=customer_id,
                provider_credit_grant_id=existing.provider_credit_grant_id,
                subscription=subscription,
                plan=plan,
                has_payment_method=existing.payment_method_attached_at is not None,
            ),
            plan=plan,
        )

    def _customer_id(
        self,
        payments: PaymentProvider,
        existing: BillingAccount,
        *,
        user_id: str,
        workspace_id: str,
    ) -> str:
        """Who the provider bills for this person, registering them if needed."""

        if existing.provider_customer_id:
            return existing.provider_customer_id
        user = UserRepository(self.session).get(user_id)
        if user is None:
            raise NotFoundError(f"no user to bill: {user_id}")
        return payments.create_customer(
            account_id=user_id, email=user.email, workspace_id=workspace_id
        ).provider_customer_id


def _provisioned(account: BillingAccount) -> bool:
    """Whether this row holds everything provisioning creates.

    All three, because provisioning is not one write and can stop between them:
    a row naming a customer and no subscription is a registration that got half
    way, and every later call has to finish it rather than read it as done.
    """

    return bool(account.provider_customer_id and account.provider_subscription_id and account.plan)


def owned_workspace_id(session: Session, user_id: str) -> str:
    """A workspace to stamp on the provider's record of this customer.

    Traceability only — the account is the person, not the workspace — but a
    payment arriving out of band is far easier to place with one attached.
    """

    owned = WorkspaceMemberRepository(session).owned_workspace_ids(user_id)
    if not owned:
        raise NotFoundError(f"no workspace to bill for user: {user_id}")
    return owned[0]


__all__ = ["BillingAccountService", "owned_workspace_id"]
