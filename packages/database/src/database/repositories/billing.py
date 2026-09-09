from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.tables.billing import BillingAccountTable
from database.tables.identity import WorkspaceMemberTable
from database.tables.orchestration import ContainerTable
from shared.billing_accounts import BillingAccount, BillingAccountStatus
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.containers import LIVE_CONTAINER_STATUSES
from shared.errors import ConflictError
from shared.identity import WorkspaceRole
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

    def page_subscribed(
        self, *, after_user_id: str | None, limit: int
    ) -> tuple[BillingAccount, ...]:
        """The accounts naming a subscription, walked by payer in one order.

        A keyset walk rather than an offset: the caller is a periodic pass that
        covers every account across several runs, and an offset would skip or
        repeat rows as accounts are added underneath it. `user_id` order is
        arbitrary and total, which is all a walk needs.

        `None` starts the walk, and it is an absent predicate rather than a
        sentinel value: the column is a native UUID, so any string standing for
        "before every id" is one the database refuses to parse.
        """

        if limit <= 0:
            return ()
        statement = select(BillingAccountTable).where(
            BillingAccountTable.provider_subscription_id != ""
        )
        if after_user_id is not None:
            statement = statement.where(BillingAccountTable.user_id > after_user_id)
        rows = self.session.scalars(
            statement.order_by(BillingAccountTable.user_id).limit(limit)
        ).all()
        return tuple(_account(row) for row in rows)

    def page_with_live_compute(
        self, *, after_user_id: str | None, limit: int
    ) -> tuple[BillingAccount, ...]:
        statement = select(BillingAccountTable).where(
            select(ContainerTable.id)
            .join(
                WorkspaceMemberTable,
                WorkspaceMemberTable.workspace_id == ContainerTable.workspace_id,
            )
            .where(
                WorkspaceMemberTable.user_id == BillingAccountTable.user_id,
                WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
                ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
            )
            .exists()
        )
        if after_user_id is not None:
            statement = statement.where(BillingAccountTable.user_id > after_user_id)
        return tuple(
            _account(row)
            for row in self.session.scalars(
                statement.order_by(BillingAccountTable.user_id).limit(limit)
            )
        )

    def upsert(
        self,
        *,
        user_id: str,
        status: BillingAccountStatus,
        provider_customer_id: str,
        provider_subscription_id: str,
        plan: BillingPlanId | None,
        subscription_terms_version: SubscriptionTermsVersion | None,
        scheduled_terms_version: SubscriptionTermsVersion | None,
        scheduled_change_at: datetime | None,
    ) -> BillingAccount:
        """Write the account for a user, creating it on the first write.

        Keyed on the user rather than on a row id, because the caller knows who
        pays and not whether anyone has recorded it yet: a sign-in registering a
        customer and a delivery settling standing arrive the same way, and only
        one of them is ever the first. For the same reason the identity is not
        the caller's to supply — a caller that minted one would write it into a
        row that already had a different one, and every later read would return
        an id naming no row.

        The caller reads the row under a lock and supplies every subscription
        field. No parameter means "leave this alone", which would let an ended
        subscription remain recorded as active. `plan` is `None` when the
        account has no recorded plan.

        `payment_method_attached_at` and `complimentary_since` are deliberately
        not among them; `set_payment_method_present` and `set_complimentary`
        write them instead. The columns here move together as one settlement of
        what the provider says an account is subscribed to; the card arrives on
        an unrelated delivery and the waiver on an administrator's decision, and
        no caller of this method reads either or has any business restating
        them. One that had to would be threading a fact it does not own through
        every call, and the first to get it wrong would erase a customer's card
        or start billing an account somebody chose not to.
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
        row.plan = plan.value if plan is not None else ""
        row.subscription_terms_version = (
            subscription_terms_version.value if subscription_terms_version is not None else None
        )
        row.scheduled_terms_version = (
            scheduled_terms_version.value if scheduled_terms_version is not None else None
        )
        row.scheduled_change_at = to_utc(scheduled_change_at) if scheduled_change_at else None
        try:
            self.session.flush()
        except IntegrityError as exc:
            # Two first writes for one user race here — two sign-ins for one
            # account in two tabs — and the loser is the one that read no row.
            # Its retry finds the winner's and updates it.
            raise ConflictError(f"billing account already exists for user: {user_id}") from exc
        return _account(row)

    def set_payment_method_present(self, *, user_id: str, present: bool, at: datetime) -> bool:
        """Record whether this account holds a card, reporting whether that moved.

        Its own write rather than part of `upsert` for the reason stated there:
        the card is settled by a delivery that knows nothing about the
        subscription, and the two must not overwrite each other.

        Takes whether there is a card, not the instant to store, because both
        callers ask the same question and neither should have to know that an
        account which already had one keeps the date it got it. Every cycle
        boundary re-asserts this, so an instant taken from the caller would walk
        forward a month at a time and the column would end up meaning "when this
        was last checked".

        `at` is used only where there was no card before. The boolean reports a
        real change — not a re-assertion — which is what makes this safe to call
        on every cycle and still worth logging when it comes back true.
        """

        row = self.session.scalars(
            select(BillingAccountTable).where(BillingAccountTable.user_id == user_id)
        ).first()
        if row is None:
            return False
        current = row.payment_method_attached_at
        if present == (current is not None):
            return False
        row.payment_method_attached_at = at if present else None
        self.session.flush()
        return True

    def set_complimentary(self, *, user_id: str, present: bool, at: datetime) -> BillingAccount:
        """Record whether an administrator has waived this account's bill.

        The caller holds the row through `lock_for_registration`, which also
        inserts it for an account that has never signed in, so a waiver can be
        granted before the person it is for has ever reached the platform. The
        same lock is the one admission takes on every start, which is what keeps
        a grant or a withdrawal from landing between the read that decides a
        container's terms and the write that admits it.

        `at` is kept only where there was no waiver before, for the reason the
        card column keeps its first instant. Re-granting is not a new decision.
        """

        row = self.session.scalars(
            select(BillingAccountTable).where(BillingAccountTable.user_id == user_id)
        ).one()
        current = row.complimentary_since
        if present != (current is not None):
            row.complimentary_since = at if present else None
            self.session.flush()
        return _account(row)

    def for_users(self, user_ids: Sequence[str]) -> dict[str, BillingAccount]:
        """The accounts behind a page of users, keyed by user; absent where none exists."""

        if not user_ids:
            return {}
        rows = self.session.scalars(
            select(BillingAccountTable).where(BillingAccountTable.user_id.in_(list(user_ids)))
        ).all()
        return {row.user_id: _account(row) for row in rows}


def _account(row: BillingAccountTable) -> BillingAccount:
    return BillingAccount(
        id=row.id,
        user_id=row.user_id,
        status=BillingAccountStatus(row.status),
        provider_customer_id=row.provider_customer_id,
        provider_subscription_id=row.provider_subscription_id,
        plan=BillingPlanId(row.plan) if row.plan else None,
        subscription_terms_version=(
            SubscriptionTermsVersion(row.subscription_terms_version)
            if row.subscription_terms_version is not None
            else None
        ),
        scheduled_terms_version=(
            SubscriptionTermsVersion(row.scheduled_terms_version)
            if row.scheduled_terms_version is not None
            else None
        ),
        scheduled_change_at=to_utc(row.scheduled_change_at) if row.scheduled_change_at else None,
        payment_method_attached_at=(
            to_utc(row.payment_method_attached_at) if row.payment_method_attached_at else None
        ),
        complimentary_since=(to_utc(row.complimentary_since) if row.complimentary_since else None),
        # The columns come back without a zone on backends that do not keep one,
        # and a naive timestamp on a payment record is a period boundary nobody
        # can place.
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


__all__ = ["BillingAccountRepository"]
