from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from database.tables.credit_purchases import CreditPurchaseTable
from shared.credit_payments import CreditPaymentStatus
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class CreditPurchaseRepository:
    session: Session

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
