from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from api.server.services import ApiServices
from billing.enforcement import BillingEnforcementService
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.identity import (
    UserRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from database.repositories.orchestration import ContainerRepository
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import FREE_PLAN_INCLUDED_NANOS, NO_CARD_INCLUDED_NANOS
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.timestamps import utc_now

CYCLE_STARTED_AT = datetime(2026, 8, 1, tzinfo=UTC)
CYCLE_ENDED_AT = datetime(2026, 9, 1, tzinfo=UTC)


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


def _account(
    services: ApiServices,
    *,
    has_card: bool,
    allowance_nanos: int,
    spent_nanos: int,
) -> tuple[str, str]:
    """An account in a cycle it has spent the given amount of, and its container."""

    with services.context.database.session() as session:
        user_id = UserRepository(session).create(display_name=f"enforce-{uuid4().hex[:8]}").id
        workspace_id = WorkspaceRepository(session).create(name=f"enforce-{uuid4()}").id
        WorkspaceMemberRepository(session).ensure_owner(workspace_id=workspace_id, user_id=user_id)
        accounts = BillingAccountRepository(session)
        accounts.upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_{user_id}",
            provider_subscription_id=f"sub_{user_id}",
            provider_credit_grant_id=f"credgr_{user_id}",
            plan=BillingPlanId.Free,
        )
        if has_card:
            accounts.set_payment_method_present(user_id=user_id, present=True, at=utc_now())
        allowances = BillingAllowanceRepository(session)
        allowances.set_subscription_period(
            user_id=user_id,
            period_started_at=CYCLE_STARTED_AT,
            period_ended_at=CYCLE_ENDED_AT,
            allowance_nanos=allowance_nanos,
            funded=True,
        )
        allowances.increment(user_id=user_id, at=CYCLE_STARTED_AT, cost_nanos=spent_nanos)
        container = ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="running",
                image="",
                command=[],
                workspace_id=workspace_id,
                status=ContainerStatus.Running,
            )
        )
        session.commit()
    return user_id, container.id


def test_only_compute_nobody_can_be_billed_for_is_stopped(
    isolated_services: ApiServices,
) -> None:
    """Overspending is invoiced. Overspending with no card is stopped.

    The whole of the distinction this sweep exists to make, and the expensive
    half to get wrong in either direction. Stopping the carded account would kill
    a paying customer's work over a bill they have not been handed yet; leaving
    the cardless one running spends real money on hardware against an invoice
    nobody will ever pay.

    Both accounts below are past their allowance by the same margin, so the only
    thing separating them is whether anybody can be charged.
    """

    _, cardless_container_id = _account(
        isolated_services,
        has_card=False,
        allowance_nanos=NO_CARD_INCLUDED_NANOS,
        spent_nanos=NO_CARD_INCLUDED_NANOS + 1,
    )
    _, carded_container_id = _account(
        isolated_services,
        has_card=True,
        allowance_nanos=FREE_PLAN_INCLUDED_NANOS,
        spent_nanos=FREE_PLAN_INCLUDED_NANOS + 1,
    )
    _, solvent_container_id = _account(
        isolated_services,
        has_card=False,
        allowance_nanos=NO_CARD_INCLUDED_NANOS,
        spent_nanos=0,
    )
    stopper = _Stopper()
    service = BillingEnforcementService(
        database=isolated_services.context.database,
        containers=stopper,
        events=isolated_services.events,
    )

    result = service.enforce(now=CYCLE_STARTED_AT)

    assert stopper.stopped == {cardless_container_id: StopContainerReason.Unfunded}
    assert carded_container_id not in stopper.stopped
    assert solvent_container_id not in stopper.stopped
    assert (result.unfunded_count, result.stopped_count, result.failed_count) == (1, 1, 0)
