from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from database.types import DatabaseSession
from shared.placement import ProductRegion


class PaymentAdmission(Protocol):
    """Billing eligibility and capacity checks owned by the billing package."""

    def assert_may_take_on_billed_work(
        self, session: DatabaseSession, *, workspace_id: str
    ) -> None: ...

    def assert_workload_eligible(
        self,
        session: DatabaseSession,
        *,
        workspace_id: str,
        gpu: Sequence[str],
        gpu_count: int,
        region: ProductRegion | None = None,
        availability_zone: str = "",
    ) -> None: ...

    def admit_container_start(
        self,
        session: DatabaseSession,
        *,
        workspace_id: str,
        gpu: Sequence[str],
        gpu_count: int,
        region: ProductRegion | None = None,
        availability_zone: str = "",
    ) -> list[str]:
        """Refuse a start the account may not make, and say which cards to ask for.

        Returns the GPU models to schedule. A request that named models gets them
        back unchanged once each is one the plan allows; a request for `any` card
        comes back narrowed to the plan's models, so the scheduler is never asked
        for hardware the account may not hold. A CPU request comes back empty. The
        caller stores what comes back on the container, because that is what the
        plan's GPU concurrency is counted from.
        """
        ...


__all__ = ["PaymentAdmission"]
