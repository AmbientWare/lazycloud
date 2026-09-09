from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

from api.server.services import ApiServices
from billing.enforcement import BillingEnforcementService
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_funding import BillingFundingRepository
from database.repositories.orchestration import ContainerRepository
from shared.billing_quotes import ContainerShape
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner
from tests.service_fixtures import workspace_owner_user_id


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


def test_expired_funding_is_stopped_even_with_a_saved_card(
    postgres_services: ApiServices,
) -> None:
    now = utc_now()
    with postgres_services.context.database.session() as session:
        workspace_id = postgres_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(postgres_services.context, workspace_id)
        BillingAccountRepository(session).set_payment_method_present(
            user_id=user_id, present=True, at=now
        )
        active_id, expired_id = str(uuid4()), str(uuid4())
        for container_id, deadline in (
            (active_id, now + timedelta(seconds=60)),
            (expired_id, now - timedelta(seconds=1)),
        ):
            ContainerRepository(session).records.upsert(
                ContainerRecord(
                    id=container_id,
                    name="funding-enforcement",
                    image="image",
                    command=["true"],
                    workspace_id=workspace_id,
                    status=ContainerStatus.Running,
                ),
                workspace_id=workspace_id,
            )
            funding = BillingFundingRepository(session)
            shape = ContainerShape(UsageBillingOwner.PlatformFleet, "", 1000, 1024, 0)
            funding.create(
                container_id=container_id,
                workspace_id=workspace_id,
                user_id=user_id,
                shape=shape,
            )
            funding.authorize(
                container_id=container_id,
                worker_id="worker",
                shape=shape,
                authorized_at=now - timedelta(seconds=60),
                valid_until=deadline,
            )
    stopper = _Stopper()
    service = BillingEnforcementService(
        database=postgres_services.context.database,
        containers=stopper,
        events=postgres_services.events,
        stop_limit_per_account=1,
    )

    result = service.enforce(now=now)

    assert stopper.stopped == {expired_id: StopContainerReason.Unfunded}
    assert (result.unfunded_count, result.stopped_count, result.failed_count) == (1, 1, 0)
