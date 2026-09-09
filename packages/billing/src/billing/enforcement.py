from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_funding import BillingFundingRepository
from database.repositories.orchestration import ContainerRepository
from shared.billing_accounts import BillingAccount, BillingAccountStatus
from shared.container_requests import StopContainerReason
from shared.errors import DomainError
from shared.events import EventLevel
from shared.funding import FUNDING_SHUTDOWN_GRACE_SECONDS
from shared.timestamps import to_utc, utc_now

from billing.funding import BillingFundingService
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
    """Stop containers with invalid funding or accounts with unpaid credit debt.

    Workers enforce permit expiry locally even when this sweep cannot reach them.
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
    recovery_cursor_user_id: str | None = field(default=None, init=False)

    def enforce(self, *, now: datetime | None = None) -> BillingEnforcementResult:
        """Walk a bounded slice of subscribed accounts and stop what is unfunded.

        One account whose containers cannot be stopped is counted and stepped
        over. A pass that stopped there would leave every account behind it
        running, which is the failure mode that costs money rather than the one
        that logs it.
        """

        moment = to_utc(now or utc_now())
        self._recover_destroyed_workers(now=moment)
        checked = unfunded = stopped = failed = 0
        while checked < self.max_accounts:
            accounts = self._page()
            if not accounts:
                self.cursor_user_id = None
                break
            for account in accounts:
                self.cursor_user_id = account.user_id
                checked += 1
                container_ids = self._unfunded_containers(account, now=moment)
                if not container_ids:
                    continue
                unfunded += 1
                account_stopped, account_failed = self._stop_containers(account, container_ids)
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

    def _recover_destroyed_workers(self, *, now: datetime) -> None:
        with self.database.session() as session:
            user_ids = BillingFundingRepository(session).recovery_accounts(
                after_user_id=self.recovery_cursor_user_id,
                expired_before=now - timedelta(seconds=FUNDING_SHUTDOWN_GRACE_SECONDS),
                limit=20,
            )
        for user_id in user_ids:
            self.recovery_cursor_user_id = user_id
            try:
                with self.database.session() as session:
                    BillingFundingService(session).reconcile_destroyed_workers(
                        user_id=user_id, now=now, limit=20
                    )
            except DomainError:
                LOGGER.exception("billing: could not reconcile destroyed workers for %s", user_id)
        if len(user_ids) < 20:
            self.recovery_cursor_user_id = None

    def _page(self) -> tuple[BillingAccount, ...]:
        with self.database.session() as session:
            return BillingAccountRepository(session).page_subscribed(
                after_user_id=self.cursor_user_id,
                limit=self.batch_limit,
            )

    def _unfunded_containers(self, account: BillingAccount, *, now: datetime) -> tuple[str, ...]:
        if account.complimentary_since is not None:
            return ()
        with self.database.session() as session:
            credits = BillingCreditRepository(session)
            cutover = credits.cutover(user_id=account.user_id)
            funding = BillingFundingService(session)
            limit = funding.get_preferences(user_id=account.user_id).monthly_usage_limit_nanos
            over_budget = False
            if limit is not None:
                budget = funding.usage_budget(user_id=account.user_id, at=now)
                over_budget = limit == 0 or budget.spent_nanos + budget.held_nanos > limit
            if (
                account.status is BillingAccountStatus.PastDue
                or account.plan is None
                or not account.provider_subscription_id
                or (credits.debt_nanos(user_id=account.user_id, at=now) > 0)
                or cutover is None
                or cutover.completed_at is None
                or over_budget
            ):
                return tuple(
                    ContainerRepository(session).live_container_ids_for_owner(
                        owner_user_id=account.user_id,
                        limit=self.stop_limit_per_account,
                    )
                )
            return BillingFundingRepository(session).unfunded_live_container_ids(
                user_id=account.user_id, at=now, limit=self.stop_limit_per_account
            )

    def _stop_containers(
        self, account: BillingAccount, container_ids: tuple[str, ...]
    ) -> tuple[int, int]:
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
                message=(f"stopped {stopped} containers without funded runtime permission"),
                data={"stopped": stopped, "failed": failed},
            )
        return stopped, failed


__all__ = [
    "UNFUNDED_COMPUTE_STOPPED_ACTION",
    "BillingEnforcementResult",
    "BillingEnforcementService",
    "ContainerStopper",
]
