from __future__ import annotations

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
    first; a container carries a concurrency bound nothing else does, so only a
    container has a second method. A new billable resource calls the first and
    adds nothing here.

    Every question is asked inside the transaction that would create the thing,
    so a refusal leaves nothing to undo.
    """

    def assert_may_take_on_billed_work(
        self, session: DatabaseSession, *, workspace_id: str
    ) -> None: ...

    def assert_may_start_container(
        self, session: DatabaseSession, *, workspace_id: str
    ) -> None: ...


__all__ = ["PaymentAdmission"]
