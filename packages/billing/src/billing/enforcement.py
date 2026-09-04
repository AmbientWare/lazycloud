from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.orchestration import ContainerRepository
from shared.billing_accounts import BillingAccount
from shared.container_requests import StopContainerReason
from shared.errors import DomainError
from shared.events import EventLevel
from shared.timestamps import to_utc, utc_now

from billing.sweeps import BillingEventSink
from database import DatabaseClient

LOGGER = logging.getLogger(__name__)

UNFUNDED_COMPUTE_STOPPED_ACTION = "billing.unfunded_compute.stopped"


class ContainerStopper(Protocol):
    """The one thing this sweep does to the world."""

    def stop(
        self,
        container_id: str,
        *,
        reason: StopContainerReason = StopContainerReason.User,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class BillingEnforcementResult:
    """What one pass looked at, and what it stopped."""

    accounts_checked: int = 0
    unfunded_count: int = 0
    stopped_count: int = 0
    failed_count: int = 0


@dataclass(slots=True)
class BillingEnforcementService:
    """Stop compute for accounts that have spent what nobody will pay for.

    Admission refuses the container that has not started. This is the other
    half: a container already running goes on costing money every second, and
    for an account with no card that money is not billed late — it is lost.

    Only ever cardless accounts. An account with a card that runs past its
    allowance is a customer to invoice, and stopping their work over a bill they
    have not been given the chance to pay is the opposite of what this platform
    sells. A card that stopped working is `PastDue`, which refuses *new* work
    and leaves what is running alone, deliberately.

    Which accounts those are is read from the local row, so a card removed
    mid-cycle does not reach this until the cycle boundary re-asks the provider.
    That is the rule the owner chose — a card being swapped must not kill live
    work — and it falls out of where the fact is written rather than needing a
    grace period here.

    This is the control that actually bounds the loss, not admission: usage
    reaches the ledger on an interval, so an account is always some fraction of
    that interval past whatever it has been measured at, and the interval between
    passes here adds to it directly. It is meant to run on every tick.
    """

    database: DatabaseClient
    containers: ContainerStopper
    events: BillingEventSink
    batch_limit: int = 100
    max_accounts: int = 500
    stop_limit_per_account: int = 50
    """How many containers one account may have stopped in a single pass.

    A bound on the work one pass does, not on what an account may hold: what is
    left is stopped by the next pass, seconds later. Without it a single account
    holding its full concurrency limit would spend the whole tick, and every
    other unfunded account would wait behind it — which is the starvation shape
    this sweep exists to avoid, not to create.
    """

    cursor_user_id: str | None = field(default=None, init=False)
    """Where the last pass stopped, `None` at the start of the walk."""

    def enforce(self, *, now: datetime | None = None) -> BillingEnforcementResult:
        """Walk a bounded slice of subscribed accounts and stop what is unfunded.

        One account whose containers cannot be stopped is counted and stepped
        over. A pass that stopped there would leave every account behind it
        running, which is the failure mode that costs money rather than the one
        that logs it.
        """

        moment = to_utc(now or utc_now())
        checked = unfunded = stopped = failed = 0
        while checked < self.max_accounts:
            accounts = self._page()
            if not accounts:
                self.cursor_user_id = None
                break
            for account in accounts:
                self.cursor_user_id = account.user_id
                checked += 1
                if not self._is_unfunded(account, now=moment):
                    continue
                unfunded += 1
                account_stopped, account_failed = self._stop_everything(account)
                stopped += account_stopped
                failed += account_failed
                if checked >= self.max_accounts:
                    break
        if stopped:
            LOGGER.warning(
                "billing: stopped %d containers across %d accounts with nothing left to spend",
                stopped,
                unfunded,
            )
        return BillingEnforcementResult(
            accounts_checked=checked,
            unfunded_count=unfunded,
            stopped_count=stopped,
            failed_count=failed,
        )

    def _page(self) -> tuple[BillingAccount, ...]:
        with self.database.session() as session:
            return BillingAccountRepository(session).page_subscribed(
                after_user_id=self.cursor_user_id,
                limit=self.batch_limit,
            )

    def _is_unfunded(self, account: BillingAccount, *, now: datetime) -> bool:
        """Whether this account may not keep what it is running.

        A cycle nothing covers counts as unfunded for the same reason admission
        refuses on it: there are no terms to spend against, and the seam it
        happens in is measured in the minutes between a cycle ending at the
        provider and the delivery that opens the next one here.
        """

        if (
            account.payment_method_attached_at is not None
            or account.complimentary_since is not None
        ):
            return False
        with self.database.session() as session:
            spent = BillingAllowanceRepository(session).current_period(
                user_id=account.user_id, at=now
            )
        return spent is None or spent.remaining_nanos <= 0

    def _stop_everything(self, account: BillingAccount) -> tuple[int, int]:
        """Stop what this account is holding, reporting what worked and what did not."""

        with self.database.session() as session:
            container_ids = ContainerRepository(session).live_container_ids_for_owner(
                owner_user_id=account.user_id,
                limit=self.stop_limit_per_account,
            )
        if not container_ids:
            return 0, 0
        stopped = failed = 0
        for container_id in container_ids:
            try:
                self.containers.stop(container_id, reason=StopContainerReason.Unfunded)
            except DomainError:
                # One container that will not stop says nothing about the rest,
                # and the next pass tries it again in seconds.
                LOGGER.exception("billing: could not stop unfunded container %s", container_id)
                failed += 1
                continue
            stopped += 1
        if stopped:
            self.events.emit(
                UNFUNDED_COMPUTE_STOPPED_ACTION,
                resource_type="billing_account",
                resource_id=account.user_id,
                level=EventLevel.Warning,
                message=(
                    f"stopped {stopped} containers for an account with no payment method "
                    f"that has spent what it was given"
                ),
                data={"stopped": stopped, "failed": failed},
            )
        return stopped, failed


__all__ = [
    "UNFUNDED_COMPUTE_STOPPED_ACTION",
    "BillingEnforcementResult",
    "BillingEnforcementService",
    "ContainerStopper",
]
