from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.orchestration import ContainerRepository
from shared.billing_accounts import BillingAccount, BillingAccountStatus
from shared.container_requests import StopContainerReason
from shared.errors import DomainError
from shared.events import EventLevel
from shared.timestamps import to_utc, utc_now

from billing.preferences import BillingPreferencesService
from billing.sweeps import BillingEventSink
from database import DatabaseClient

LOGGER = logging.getLogger(__name__)

UNFUNDED_COMPUTE_STOPPED_ACTION = "billing.unfunded_compute.stopped"


class ContainerStopper(Protocol):
    def stop(
        self,
        container_id: str,
        *,
        reason: StopContainerReason = StopContainerReason.User,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class BillingEnforcementResult:
    accounts_checked: int = 0
    unfunded_count: int = 0
    stopped_count: int = 0
    failed_count: int = 0


@dataclass(slots=True)
class BillingEnforcementService:
    """Stop compute when recorded usage exhausts the account's credit or usage budget."""

    database: DatabaseClient
    containers: ContainerStopper
    events: BillingEventSink
    batch_limit: int = 100
    max_accounts: int = 500
    stop_limit_per_account: int = 50
    """Bound work per account so one large account cannot stall the sweep."""

    cursor_user_id: str | None = field(default=None, init=False)

    def enforce(self, *, now: datetime | None = None) -> BillingEnforcementResult:
        """Check accounts with live compute, including canceled subscriptions."""

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

    def _page(self) -> tuple[BillingAccount, ...]:
        with self.database.session() as session:
            return BillingAccountRepository(session).page_with_live_compute(
                after_user_id=self.cursor_user_id,
                limit=self.batch_limit,
            )

    def _unfunded_containers(self, account: BillingAccount, *, now: datetime) -> tuple[str, ...]:
        if account.complimentary_since is not None:
            return ()
        with self.database.session() as session:
            credits = BillingCreditRepository(session)
            preferences = BillingPreferencesService(session)
            limit = preferences.get(user_id=account.user_id).monthly_usage_limit_nanos
            over_budget = False
            if limit is not None:
                budget = preferences.usage_budget(user_id=account.user_id, at=now)
                over_budget = budget.spent_nanos >= limit
            if (
                account.status is BillingAccountStatus.PastDue
                or account.plan is None
                or not account.provider_subscription_id
                or credits.balance(user_id=account.user_id, at=now) <= 0
                or over_budget
            ):
                return tuple(
                    ContainerRepository(session).live_container_ids_for_owner(
                        owner_user_id=account.user_id,
                        limit=self.stop_limit_per_account,
                    )
                )
            return ()

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
                message=f"stopped {stopped} containers after account credit or usage limit ran out",
                data={"stopped": stopped, "failed": failed},
            )
        return stopped, failed


__all__ = [
    "UNFUNDED_COMPUTE_STOPPED_ACTION",
    "BillingEnforcementResult",
    "BillingEnforcementService",
    "ContainerStopper",
]
