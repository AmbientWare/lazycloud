from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from database.tables.billing import BillingAccountTable
from shared.billing_accounts import BillingAccount, BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.errors import ConflictError
from shared.timestamps import to_utc
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class BillingAccountRepository:
    session: Session

    def get_by_user(self, user_id: str, *, for_update: bool = False) -> BillingAccount | None:
        """The account for a payer, who is who the provider invoices.

        Locked where the caller is about to decide the account's standing from
        what it reads. Without the lock, two deliveries about the same account
        interleave: each reads the standing the other has not committed yet, and
        one of them concludes there is nothing to change.

        A locked read is only worth taking if it returns what is committed now,
        so it overwrites whatever this session loaded earlier — the values a
        caller waited on the lock to see are exactly the ones the identity map
        would otherwise hand back stale.
        """

        statement = select(BillingAccountTable).where(BillingAccountTable.user_id == user_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = self.session.scalars(statement).first()
        return _account(row) if row is not None else None

    def lock_for_registration(self, user_id: str) -> BillingAccount:
        """The account row for a payer, created if it is missing and locked either way.

        `SELECT ... FOR UPDATE` locks rows that exist, and a first registration
        has none. Two of them would both read nothing, both register a customer
        at the payment provider, and the unique constraint would then refuse one
        of the two — one person unable to sign in, and a customer left at the
        provider that nothing here will ever name again. Inserting the row before
        locking it is what gives the lock something to hold, so the second caller
        waits on the first and reads what the first registered.

        The row this inserts names no customer yet. That is what a registration
        which has not finished looks like, and the next call for the same account
        finishes it.
        """

        dialect = self.session.get_bind().dialect.name
        statement = (
            postgresql_insert(BillingAccountTable)
            if dialect == "postgresql"
            else sqlite_insert(BillingAccountTable)
        )
        self.session.execute(
            statement.values(
                id=str(uuid4()),
                user_id=user_id,
                status=BillingAccountStatus.Active.value,
                provider_customer_id="",
                provider_subscription_id="",
                provider_credit_grant_id="",
                plan="",
            ).on_conflict_do_nothing(index_elements=[BillingAccountTable.user_id])
        )
        row = self.session.scalars(
            select(BillingAccountTable)
            .where(BillingAccountTable.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        return _account(row)

    def get_by_provider_customer(
        self, provider_customer_id: str, *, for_update: bool = False
    ) -> BillingAccount | None:
        """The account behind a payment-provider customer.

        The direction a notification forces: an event about a saved card names the
        customer it happened to and nothing else, so this is the only way back to
        whose account it is. Refuses an empty id rather than matching the accounts
        that have no payment relationship yet, which all share it — and the
        partial unique index on the column is what makes the answer single.

        Locked for the same reason `get_by_user` is: two deliveries about one
        account arrive together — a paid invoice and the renewal that follows it —
        and each would otherwise decide standing from a row the other has not
        committed yet.
        """

        if not provider_customer_id:
            return None
        statement = select(BillingAccountTable).where(
            BillingAccountTable.provider_customer_id == provider_customer_id
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row = self.session.scalars(statement).first()
        return _account(row) if row is not None else None

    def upsert(
        self,
        *,
        user_id: str,
        status: BillingAccountStatus,
        provider_customer_id: str,
        provider_subscription_id: str,
        provider_credit_grant_id: str,
        plan: BillingPlanId | None,
    ) -> BillingAccount:
        """Write the account for a user, creating it on the first write.

        Keyed on the user rather than on a row id, because the caller knows who
        pays and not whether anyone has recorded it yet: a sign-in registering a
        customer and a delivery settling standing arrive the same way, and only
        one of them is ever the first. For the same reason the identity is not
        the caller's to supply — a caller that minted one would write it into a
        row that already had a different one, and every later read would return
        an id naming no row.

        Every column is stated, and none of them has a value that means "leave
        this alone". A caller reaching here has read the row under the lock it
        holds, so it knows all of them; a parameter that could be omitted is one
        a caller omits by accident, and the value it then keeps is a subscription
        that ended or a grant that was expired — both of which read as live to
        everything downstream. `plan` is `None` for an account on no plan, which
        is what an account holds before it is provisioned and again once its
        subscription ends.
        """

        row = self.session.scalars(
            select(BillingAccountTable).where(BillingAccountTable.user_id == user_id)
        ).first()
        if row is None:
            row = BillingAccountTable(id=str(uuid4()), user_id=user_id)
            self.session.add(row)
        row.status = status.value
        row.provider_customer_id = provider_customer_id
        row.provider_subscription_id = provider_subscription_id
        row.provider_credit_grant_id = provider_credit_grant_id
        row.plan = plan.value if plan is not None else ""
        try:
            self.session.flush()
        except IntegrityError as exc:
            # Two first writes for one user race here — two sign-ins for one
            # account in two tabs — and the loser is the one that read no row.
            # Its retry finds the winner's and updates it.
            raise ConflictError(f"billing account already exists for user: {user_id}") from exc
        return _account(row)


def _account(row: BillingAccountTable) -> BillingAccount:
    return BillingAccount(
        id=row.id,
        user_id=row.user_id,
        status=BillingAccountStatus(row.status),
        provider_customer_id=row.provider_customer_id,
        provider_subscription_id=row.provider_subscription_id,
        provider_credit_grant_id=row.provider_credit_grant_id,
        plan=BillingPlanId(row.plan) if row.plan else None,
        # The columns come back without a zone on backends that do not keep one,
        # and a naive timestamp on a payment record is a period boundary nobody
        # can place.
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


__all__ = ["BillingAccountRepository"]
