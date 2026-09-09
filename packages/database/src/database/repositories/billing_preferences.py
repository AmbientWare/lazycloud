from dataclasses import dataclass
from datetime import datetime, timedelta

from database.repositories.billing import BillingAccountRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_preferences import BillingPreferencesTable
from shared.billing_preferences import AutomaticReloadPauseReason
from shared.billing_quotes import BilledDimension
from shared.errors import NotFoundError
from shared.http.billing_preferences import BillingPreferences
from shared.timestamps import to_utc
from sqlalchemy import Numeric, cast, func, or_, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class AutomaticReloadState:
    paused_purchase_id: str | None
    pause_reason: AutomaticReloadPauseReason | None
    resumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class BillingPreferencesRepository:
    session: Session

    def reload_state(self, user_id: str) -> AutomaticReloadState:
        row = self.session.get(BillingPreferencesTable, user_id)
        if row is None:
            return AutomaticReloadState(None, None, None)
        return AutomaticReloadState(
            row.reload_paused_purchase_id,
            AutomaticReloadPauseReason(row.reload_pause_reason)
            if row.reload_pause_reason
            else None,
            to_utc(row.reload_resumed_at) if row.reload_resumed_at else None,
        )

    def pause_reload(
        self,
        *,
        user_id: str,
        purchase_id: str,
        reason: AutomaticReloadPauseReason,
    ) -> None:
        row = self._row(user_id)
        row.reload_paused_purchase_id = purchase_id
        row.reload_pause_reason = reason.value
        self.session.flush()

    def resume_reload(self, *, user_id: str, at: datetime) -> None:
        row = self._row(user_id)
        row.reload_paused_purchase_id = None
        row.reload_pause_reason = ""
        row.reload_resumed_at = at
        self.session.flush()

    def mark_reload_checked(self, *, user_id: str, at: datetime) -> None:
        self._row(user_id).reload_checked_at = at
        self.session.flush()

    def due_reloads(self, *, at: datetime, limit: int) -> tuple[str, ...]:
        return tuple(
            self.session.scalars(
                select(BillingPreferencesTable.user_id)
                .where(
                    BillingPreferencesTable.reload_enabled.is_(True),
                    BillingPreferencesTable.reload_paused_purchase_id.is_(None),
                    or_(
                        BillingPreferencesTable.reload_checked_at.is_(None),
                        BillingPreferencesTable.reload_checked_at <= at - timedelta(seconds=5),
                    ),
                )
                .order_by(
                    BillingPreferencesTable.reload_checked_at.asc().nulls_first(),
                    BillingPreferencesTable.user_id,
                )
                .limit(limit)
            )
        )

    def get(self, user_id: str) -> BillingPreferences:
        row = self.session.get(BillingPreferencesTable, user_id)
        return BillingPreferences.model_validate(row) if row is not None else BillingPreferences()

    def set(self, user_id: str, preferences: BillingPreferences) -> BillingPreferences:
        if BillingAccountRepository(self.session).get_by_user(user_id, for_update=True) is None:
            raise NotFoundError("the billing account does not exist")
        row = self.session.get(BillingPreferencesTable, user_id)
        if row is None:
            row = BillingPreferencesTable(user_id=user_id)
            self.session.add(row)
        row.monthly_usage_limit_nanos = preferences.monthly_usage_limit_nanos
        row.reload_enabled = preferences.reload_enabled
        row.reload_threshold_cents = preferences.reload_threshold_cents
        row.reload_amount_cents = preferences.reload_amount_cents
        row.reload_monthly_payment_limit_cents = preferences.reload_monthly_payment_limit_cents
        self.session.flush()
        return BillingPreferences.model_validate(row)

    def gross_usage(
        self, *, user_id: str, start: datetime, end: datetime, container_id: str | None = None
    ) -> int:
        segment = BillingLedgerSegmentTable
        duration = func.extract("epoch", segment.segment_ended_at - segment.segment_started_at)
        cost = cast(segment.cost_nanos, Numeric(asdecimal=True))
        upper = func.extract(
            "epoch", func.least(segment.segment_ended_at, end) - segment.segment_started_at
        )
        lower = func.extract(
            "epoch", func.greatest(segment.segment_started_at, start) - segment.segment_started_at
        )
        statement = select(
            func.coalesce(
                func.sum(func.floor(cost * upper / duration) - func.floor(cost * lower / duration)),
                0,
            )
        ).where(
            segment.owner_user_id == user_id,
            segment.segment_started_at < end,
            segment.segment_ended_at > start,
        )
        if container_id is not None:
            statement = statement.where(
                segment.subject_type == "container",
                segment.subject_id == container_id,
                segment.dimension == BilledDimension.ComputeRuntime.value,
            )
        return int(self.session.scalar(statement) or 0)

    def _row(self, user_id: str) -> BillingPreferencesTable:
        row = self.session.get(BillingPreferencesTable, user_id)
        if row is None:
            raise NotFoundError("save automatic reload settings before managing reloads")
        return row


__all__ = ["BillingPreferencesRepository"]
