from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from database.client import DatabaseClient
from shared.payments import CreditPurchasePaymentProvider

from billing.purchases import CreditPurchaseService


@dataclass(frozen=True, slots=True)
class BillingPaymentMaintenance:
    database: DatabaseClient
    payments: Callable[[], CreditPurchasePaymentProvider]

    def maintain(self, *, now: datetime | None = None) -> None:
        CreditPurchaseService(self.database, self.payments).sweep(limit=20)


__all__ = ["BillingPaymentMaintenance"]
