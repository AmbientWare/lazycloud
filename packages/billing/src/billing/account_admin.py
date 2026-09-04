from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_costs import BillingLedgerCostRepository
from database.repositories.identity import UserRepository
from pydantic import ValidationError
from shared.billing_accounts import BillingAccount
from shared.contracts import ContractModel
from shared.errors import InvalidInputError, NotFoundError
from shared.events import EventLevel
from shared.identity import UserRecord
from shared.timestamps import to_utc
from sqlalchemy.orm import Session

from billing.sweeps import BillingEventSink

MAX_ACCOUNT_PAGE = 200

RECENT_COST_WINDOW = timedelta(days=30)
"""How far back the spend beside each account reaches.

A fixed trailing window rather than the account's billing cycle, because the
list compares accounts and each one's cycle opens on a different day.
"""

COMPLIMENTARY_CHANGED_ACTION = "billing.complimentary.changed"
BILLING_ACCOUNT_RESOURCE_TYPE = "billing_account"


@dataclass(frozen=True, slots=True)
class AdministeredAccount:
    """One person as an administrator sees them: who they are and what they owe."""

    user: UserRecord
    account: BillingAccount | None
    recent_cost_nanos: int


@dataclass(frozen=True, slots=True)
class AdministeredAccountPage:
    rows: tuple[AdministeredAccount, ...]
    next: str
    recent_cost_since: datetime


class _AccountCursorPayload(ContractModel):
    user_id: str


def encode_account_cursor(user_id: str) -> str:
    payload = _AccountCursorPayload(user_id=user_id).model_dump_json()
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_account_cursor(value: str | None) -> str | None:
    if not value:
        return None
    try:
        payload = _AccountCursorPayload.model_validate_json(
            base64.urlsafe_b64decode(value.encode()), strict=True
        )
    except (binascii.Error, ValidationError, UnicodeDecodeError) as exc:
        raise InvalidInputError("invalid account cursor") from exc
    return payload.user_id


@dataclass(frozen=True, slots=True)
class BillingAccountAdminService:
    """Every account on the platform, and the one billing decision an administrator makes.

    Walks `users` rather than `billing_accounts`, because the people an
    administrator needs to see include the ones billing has never written a row
    for: an account created through the users route that has not signed in
    yet, which is exactly the one a waiver is granted to ahead of time.
    """

    session: Session

    def list(
        self, *, after_user_id: str | None, limit: int, at: datetime
    ) -> AdministeredAccountPage:
        if limit <= 0 or limit > MAX_ACCOUNT_PAGE:
            raise InvalidInputError(f"limit must be between 1 and {MAX_ACCOUNT_PAGE}")
        since = to_utc(at) - RECENT_COST_WINDOW
        # One more than asked, so the page knows whether a next one exists
        # without a second read.
        users = UserRepository(self.session).page(after_user_id=after_user_id, limit=limit + 1)
        has_more = len(users) > limit
        users = users[:limit]
        user_ids = [user.id for user in users]
        accounts = BillingAccountRepository(self.session).for_users(user_ids)
        costs = BillingLedgerCostRepository(self.session).owner_window_totals(
            owner_user_ids=user_ids, start=since, end=to_utc(at)
        )
        rows = tuple(
            AdministeredAccount(
                user=user,
                account=accounts.get(user.id),
                recent_cost_nanos=costs.get(user.id, 0),
            )
            for user in users
        )
        return AdministeredAccountPage(
            rows=rows,
            next=encode_account_cursor(users[-1].id) if has_more and users else "",
            recent_cost_since=since,
        )

    def get(self, *, user_id: str, at: datetime) -> AdministeredAccount:
        user = UserRepository(self.session).get(user_id)
        if user is None:
            raise NotFoundError(f"user not found: {user_id}")
        since = to_utc(at) - RECENT_COST_WINDOW
        costs = BillingLedgerCostRepository(self.session).owner_window_totals(
            owner_user_ids=[user_id], start=since, end=to_utc(at)
        )
        return AdministeredAccount(
            user=user,
            account=BillingAccountRepository(self.session).get_by_user(user_id),
            recent_cost_nanos=costs.get(user_id, 0),
        )

    def set_complimentary(
        self,
        events: BillingEventSink,
        *,
        user_id: str,
        present: bool,
        at: datetime,
    ) -> AdministeredAccount:
        """Waive this account's bill, or stop waiving it.

        Takes the registration lock first, which is what inserts the row for an
        account that has never signed in and what serializes the write against
        every container start admission is deciding at the same moment. The
        event is money given away or taken back, so it is written whether or not
        the column moved. A second grant is a second decision worth a record.
        """

        user = UserRepository(self.session).get(user_id)
        if user is None:
            raise NotFoundError(f"user not found: {user_id}")
        accounts = BillingAccountRepository(self.session)
        accounts.lock_for_registration(user_id)
        account = accounts.set_complimentary(user_id=user_id, present=present, at=at)
        events.emit(
            COMPLIMENTARY_CHANGED_ACTION,
            resource_type=BILLING_ACCOUNT_RESOURCE_TYPE,
            resource_id=user_id,
            message=(
                "billing: usage is now complimentary for this account"
                if present
                else "billing: usage is no longer complimentary for this account"
            ),
            level=EventLevel.Warning,
            data={"complimentary": present, "user_id": user_id},
        )
        since = to_utc(at) - RECENT_COST_WINDOW
        costs = BillingLedgerCostRepository(self.session).owner_window_totals(
            owner_user_ids=[user_id], start=since, end=to_utc(at)
        )
        return AdministeredAccount(
            user=user, account=account, recent_cost_nanos=costs.get(user_id, 0)
        )


__all__ = [
    "COMPLIMENTARY_CHANGED_ACTION",
    "MAX_ACCOUNT_PAGE",
    "RECENT_COST_WINDOW",
    "AdministeredAccount",
    "AdministeredAccountPage",
    "BillingAccountAdminService",
    "decode_account_cursor",
    "encode_account_cursor",
]
