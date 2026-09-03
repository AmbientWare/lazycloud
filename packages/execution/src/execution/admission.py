from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from database.types import DatabaseSession


class PaymentAdmission(Protocol):
    """Whether this workspace's account may take on something it will be billed for.

    One protocol rather than one per resource, because there is one answer behind
    both questions — will what this brings into existence reach an invoice
    somebody pays — and a second protocol would be a second place to argue it.
    Execution states the shape; the account-level answer lives in the billing
    package, which this package does not import.

    Split by what is asked rather than by what asks. Everything billed asks the
    first; a container carries concurrency bounds and a GPU entitlement nothing
    else does, so only a container has a second method. A new billable resource
    calls the first and adds nothing here.

    Every question is asked inside the transaction that would create the thing,
    so a refusal leaves nothing to undo.
    """

    def assert_may_take_on_billed_work(
        self, session: DatabaseSession, *, workspace_id: str
    ) -> None: ...

    def admit_container_start(
        self,
        session: DatabaseSession,
        *,
        workspace_id: str,
        gpu: Sequence[str],
        gpu_count: int,
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
