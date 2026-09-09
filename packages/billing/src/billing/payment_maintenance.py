from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from database.client import DatabaseClient
from shared.payments import CreditPurchasePaymentProvider

from billing.automatic_reload import AutomaticReloadService
from billing.purchases import CreditPurchaseService


@dataclass(frozen=True, slots=True)
class BillingPaymentMaintenance:
    database: DatabaseClient
    payments: Callable[[], CreditPurchasePaymentProvider]

    def maintain(self, *, now: datetime | None = None) -> None:
        CreditPurchaseService(self.database, self.payments).sweep(limit=20)
        AutomaticReloadService(self.database, self.payments).sweep(limit=20, now=now)


__all__ = ["BillingPaymentMaintenance"]
