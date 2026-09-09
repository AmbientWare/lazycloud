from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

from api.server.services import ApiServices
from billing.enforcement import BillingEnforcementService
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.orchestration import ContainerRepository
from shared.billing_credits import CreditGrant, CreditKind
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.timestamps import utc_now
from tests.service_fixtures import unfunded_billing_account


@dataclass(slots=True)
class _Stopper:
    stopped: dict[str, StopContainerReason] = field(default_factory=dict)

    def stop(
        self,
        container_id: str,
        *,
        reason: StopContainerReason = StopContainerReason.User,
    ) -> object:
        self.stopped[container_id] = reason
        return None


def test_monitor_stops_empty_accounts_and_canceled_subscriptions_with_live_compute(
    postgres_services: ApiServices,
) -> None:
    now = utc_now()
    user_id, workspace_id = unfunded_billing_account(
        postgres_services.context,
        period_started_at=now,
        period_ended_at=now + timedelta(days=30),
    )
    container_id = str(uuid4())
    with postgres_services.context.database.session() as session:
        accounts = BillingAccountRepository(session)
        accounts.set_payment_method_present(user_id=user_id, present=True, at=now)
        credits = BillingCreditRepository(session)
        lot_id = credits.issue(
            user_id=user_id,
            grant=CreditGrant("payment:monitor", CreditKind.Purchased, 10**9, now),
        )
        ContainerRepository(session).records.upsert(
            ContainerRecord(
                id=container_id,
                name="credit-enforcement",
                image="image",
                command=["true"],
                workspace_id=workspace_id,
                status=ContainerStatus.Running,
            ),
            workspace_id=workspace_id,
        )
    stopper = _Stopper()
    service = BillingEnforcementService(
        database=postgres_services.context.database,
        containers=stopper,
        events=postgres_services.events,
    )
    assert service.enforce(now=now).stopped_count == 0
    with postgres_services.context.database.session() as session:
        BillingCreditRepository(session).adjust(
            user_id=user_id,
            credit_lot_id=lot_id,
            source_id="refund:monitor",
            amount_nanos=-(10**9),
            effective_at=now,
        )
    result = service.enforce(now=now)
    assert stopper.stopped == {container_id: StopContainerReason.Unfunded}
    assert (result.unfunded_count, result.stopped_count, result.failed_count) == (1, 1, 0)
    with postgres_services.context.database.session() as session:
        BillingCreditRepository(session).issue(
            user_id=user_id,
            grant=CreditGrant("payment:monitor-refill", CreditKind.Purchased, 10**9, now),
        )
    assert service.enforce(now=now).stopped_count == 0
    with postgres_services.context.database.session() as session:
        accounts = BillingAccountRepository(session)
        account = accounts.get_by_user(user_id)
        assert account is not None
        accounts.upsert(
            user_id=user_id,
            status=account.status,
            provider_customer_id=account.provider_customer_id,
            provider_subscription_id="",
            plan=None,
            subscription_terms_version=None,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
    assert service.enforce(now=now).stopped_count == 1
