from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from database.tables.credit_purchases import CreditPurchaseTable
from shared.credit_payments import CreditPaymentStatus, CreditPurchaseKind
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class CreditPurchaseRepository:
    session: Session

    def pending_automatic(self, *, user_id: str) -> CreditPurchaseTable | None:
        return self.session.scalar(
            select(CreditPurchaseTable).where(
                CreditPurchaseTable.user_id == user_id,
                CreditPurchaseTable.kind == CreditPurchaseKind.Automatic.value,
                CreditPurchaseTable.status.in_(
                    (
                        CreditPaymentStatus.Pending.value,
                        CreditPaymentStatus.ActionRequired.value,
                    )
                ),
            )
        )

    def automatic_payment_commitment(
        self,
        *,
        user_id: str,
        start: datetime,
        end: datetime,
    ) -> int:
        return int(
            self.session.scalar(
                select(
                    func.coalesce(
                        func.sum(CreditPurchaseTable.amount_nanos),
                        0,
                    )
                ).where(
                    CreditPurchaseTable.user_id == user_id,
                    CreditPurchaseTable.kind == CreditPurchaseKind.Automatic.value,
                    or_(
                        CreditPurchaseTable.status.in_(
                            (
                                CreditPaymentStatus.Pending.value,
                                CreditPaymentStatus.ActionRequired.value,
                            )
                        ),
                        and_(
                            CreditPurchaseTable.funded_at >= start,
                            CreditPurchaseTable.funded_at < end,
                        ),
                    ),
                )
            )
            or 0
        )

    def latest_automatic_failure(
        self,
        *,
        user_id: str,
        since: datetime | None,
    ) -> CreditPurchaseTable | None:
        statement = select(CreditPurchaseTable).where(
            CreditPurchaseTable.user_id == user_id,
            CreditPurchaseTable.kind == CreditPurchaseKind.Automatic.value,
            CreditPurchaseTable.status.in_(
                (
                    CreditPaymentStatus.Declined.value,
                    CreditPaymentStatus.ActionRequired.value,
                )
            ),
        )
        if since is not None:
            statement = statement.where(CreditPurchaseTable.creation_started_at >= since)
        return self.session.scalar(
            statement.order_by(
                CreditPurchaseTable.creation_started_at.desc(),
                CreditPurchaseTable.id.desc(),
            ).limit(1)
        )

    def get(self, *, purchase_id: str, user_id: str) -> CreditPurchaseTable | None:
        return self.session.scalar(
            select(CreditPurchaseTable).where(
                CreditPurchaseTable.id == purchase_id,
                CreditPurchaseTable.user_id == user_id,
            )
        )

    def by_request(self, *, user_id: str, request_key: str) -> CreditPurchaseTable | None:
        return self.session.scalar(
            select(CreditPurchaseTable).where(
                CreditPurchaseTable.user_id == user_id,
                CreditPurchaseTable.request_key == request_key,
            )
        )

    def by_payment(self, *, payment_id: str) -> CreditPurchaseTable | None:
        if not payment_id:
            return None
        return self.session.scalar(
            select(CreditPurchaseTable).where(CreditPurchaseTable.provider_payment_id == payment_id)
        )

    def by_id(self, purchase_id: str) -> CreditPurchaseTable | None:
        return self.session.get(CreditPurchaseTable, purchase_id)

    def add(self, purchase: CreditPurchaseTable) -> None:
        self.session.add(purchase)
        self.session.flush()

    def due(self, *, now: datetime, limit: int) -> tuple[tuple[str, str], ...]:
        rows = self.session.execute(
            select(CreditPurchaseTable.id, CreditPurchaseTable.user_id)
            .where(
                or_(
                    and_(
                        CreditPurchaseTable.status.in_(
                            (
                                CreditPaymentStatus.Pending.value,
                                CreditPaymentStatus.ActionRequired.value,
                            )
                        ),
                        CreditPurchaseTable.updated_at <= now - timedelta(minutes=1),
                    ),
                    and_(
                        CreditPurchaseTable.credit_lot_id.is_not(None),
                        CreditPurchaseTable.updated_at <= now - timedelta(days=1),
                    ),
                )
            )
            .order_by(CreditPurchaseTable.updated_at, CreditPurchaseTable.id)
            .limit(limit)
        )
        return tuple((row.id, row.user_id) for row in rows)
