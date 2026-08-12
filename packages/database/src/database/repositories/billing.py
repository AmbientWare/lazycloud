from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from database.tables.billing import BillingAccountTable
from database.tables.identity import WorkspaceMemberTable
from shared.billing_accounts import BillingAccount, BillingAccountStatus, BillingPlan
from shared.errors import ConflictError
from shared.identity import WorkspaceRole
from shared.timestamps import to_utc
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class BillingAccountRepository:
    session: Session

    def get_for_workspace_owner(self, workspace_id: str) -> BillingAccount | None:
        """The payment relationship behind a workspace, reached through its owner.

        One join rather than two lookups so the owner cannot change between them,
        and so every caller asks the question the same way.
        """

        row = self.session.scalars(
            select(BillingAccountTable)
            .join(
                WorkspaceMemberTable,
                WorkspaceMemberTable.user_id == BillingAccountTable.user_id,
            )
            .where(
                WorkspaceMemberTable.workspace_id == workspace_id,
                WorkspaceMemberTable.role == WorkspaceRole.Owner.value,
            )
        ).first()
        return BillingAccount.model_validate(row.payload) if row is not None else None

    def get_by_user(self, user_id: str, *, for_update: bool = False) -> BillingAccount | None:
        """The account for a payer, who is who a period bills.

        Locked where the caller is about to decide the account's standing from
        what it reads. Two things learn payment outcomes now — the charge at
        close and the webhook — and without the lock they interleave: each reads
        the standing the other has not committed yet, and one of them concludes
        there is nothing to change.
        """

        statement = select(BillingAccountTable).where(BillingAccountTable.user_id == user_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalars(statement).first()
        return BillingAccount.model_validate(row.payload) if row is not None else None

    def get_by_provider_customer(self, provider_customer_id: str) -> BillingAccount | None:
        """The account behind a payment-provider customer.

        The direction a notification forces: an event about a saved card names the
        customer it happened to and nothing else, so this is the only way back to
        whose account it is. Refuses an empty id rather than matching the accounts
        that have no payment relationship yet, which all share it — and the
        partial unique index on the column is what makes the answer single.
        """

        if not provider_customer_id:
            return None
        row = self.session.scalars(
            select(BillingAccountTable).where(
                BillingAccountTable.provider_customer_id == provider_customer_id
            )
        ).first()
        return BillingAccount.model_validate(row.payload) if row is not None else None

    def users_on_plans(
        self, plans: tuple[BillingPlan, ...], *, existing_before: datetime
    ) -> tuple[str, ...]:
        """Everyone holding one of these plans by a given moment.

        The close sweeps these alongside the accounts that ran something, because
        a subscription is owed whether or not anything ran. An account on a paid
        plan that was idle all month writes no usage and would otherwise never
        have a period opened for it — the month would pass unbilled and stay that
        way, since the close only ever looks at the month that just ended.

        Bounded by when the account started paying, because the close runs after
        the month it settles: an account created in November is on a paid plan by
        the time October is closed, and nothing else here would stop it being
        invoiced for a month it did not exist in.

        Standing is deliberately not a filter. An account that owes money is
        exactly one that should be invoiced, and skipping it would forgive the
        debt it is behind on.
        """

        if not plans:
            return ()
        rows = self.session.scalars(
            select(BillingAccountTable.user_id)
            .where(
                BillingAccountTable.plan.in_([plan.value for plan in plans]),
                BillingAccountTable.created_at < existing_before,
            )
            .order_by(BillingAccountTable.user_id.asc())
        ).all()
        return tuple(rows)

    def upsert(
        self,
        *,
        user_id: str,
        plan: BillingPlan,
        status: BillingAccountStatus,
        provider_customer_id: str = "",
    ) -> BillingAccount:
        """Write the account for a user, creating it the first time they pay.

        Keyed on the user rather than on a row id, because the caller knows who
        is paying and not whether anyone has recorded it yet — which is the whole
        question a first payment answers. For the same reason the identity is not
        the caller's to supply: a caller that minted one would write it into the
        payload of a row that already had a different one, and every later read
        would return an id naming no row.
        """

        row = self.session.scalars(
            select(BillingAccountTable).where(BillingAccountTable.user_id == user_id)
        ).first()
        if row is None:
            row = BillingAccountTable(id=str(uuid4()), user_id=user_id, payload={})
            self.session.add(row)
        row.plan = plan.value
        row.status = status.value
        row.provider_customer_id = provider_customer_id
        try:
            self.session.flush()
        except IntegrityError as exc:
            # Two first payments for one user race here, and the loser is the one
            # that read no row. Its retry finds the winner's and updates it.
            raise ConflictError(f"billing account already exists for user: {user_id}") from exc
        account = BillingAccount(
            id=row.id,
            user_id=row.user_id,
            plan=plan,
            status=status,
            provider_customer_id=provider_customer_id,
            # The column comes back without a zone on backends that do not keep
            # one, and a naive timestamp on a payment record is a period boundary
            # nobody can place.
            created_at=to_utc(row.created_at),
            updated_at=to_utc(row.updated_at),
        )
        row.payload = account.model_dump(mode="json")
        self.session.flush()
        return account


__all__ = ["BillingAccountRepository"]
