from __future__ import annotations

from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.billing import BillingAccountRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.execution import TaskTable
from database.tables.orchestration import ContainerTable
from execution.functions.service import FunctionControlService
from pydantic import JsonValue
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_rate_card import account_terms
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import CapacityLimitReachedError, PaymentRequiredError
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import FunctionInvokeBody
from sqlalchemy import func, select
from tests.workspaces import workspace_owner_user_id


def test_invoking_a_function_past_due_refuses_and_queues_nothing(
    isolated_services: ApiServices,
) -> None:
    """The refusal has to land before the task, not around the container.

    Invoking commits the task and records its usage before anything is
    scheduled, so a gate that only guarded the container reservation would roll
    back the reservation and leave the task behind — queued forever, for an
    account nothing will ever schedule, in the customer's own task list.

    And it has to reach the caller as a refusal. This path answers most failures
    with a finished task carrying the message, which is right for code that ran
    and went wrong; it is wrong for the platform declining to run anything, where
    there is no task for the answer to live on.
    """

    stub = ControlPlaneService(
        isolated_services.context,
    ).create_stub(
        "past-due-function",
        kind=StubKind.Function,
        handler="pkg.workloads:handler",
        config={"image": {"image_id": "image-past-due"}},
    )
    _past_due(isolated_services, stub.workspace_id)

    before = _row_counts(isolated_services, stub.workspace_id)
    with pytest.raises(PaymentRequiredError, match="did not go through"):
        FunctionControlService(isolated_services).function_invoke(
            FunctionInvokeBody(
                stub_id=stub.id,
                invocation=FunctionJsonInvocation(args=[1]),
            )
        )
    assert _row_counts(isolated_services, stub.workspace_id) == before


@pytest.mark.parametrize("placement", [{"region": "eu-central"}, {"availability_zone": "use1-az5"}])
def test_free_function_pinned_placement_refuses_before_creating_work(
    isolated_services: ApiServices,
    placement: dict[str, JsonValue],
) -> None:
    stub = ControlPlaneService(
        isolated_services.context,
    ).create_stub(
        "regional-function",
        kind=StubKind.Function,
        handler="pkg.workloads:handler",
        config={"runtime": placement},
    )
    before = _row_counts(isolated_services, stub.workspace_id)
    with pytest.raises(PaymentRequiredError, match="selection requires the Team plan"):
        FunctionControlService(isolated_services).function_invoke(
            FunctionInvokeBody(stub_id=stub.id, invocation=FunctionJsonInvocation(args=[]))
        )
    assert _row_counts(isolated_services, stub.workspace_id) == before


def test_capacity_limit_allows_warm_invocation_but_refuses_cold_without_a_task(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    warm = control.create_stub(
        "warm-at-limit", kind=StubKind.Function, handler="pkg.workloads:handler"
    )
    cold = control.create_stub(
        "cold-at-limit", kind=StubKind.Function, handler="pkg.workloads:handler"
    )
    user_id = workspace_owner_user_id(isolated_services.context, warm.workspace_id)
    with isolated_services.context.database.session() as session:
        account = BillingAccountRepository(session).get_by_user(user_id)
        assert account is not None and account.plan is not None
        limit = account_terms(
            account.plan, has_payment_method=account.payment_method_attached_at is not None
        ).entitlements.max_concurrent_cpu_containers
        for index in range(limit):
            ContainerRepository(session).upsert(
                ContainerRecord(
                    id=str(uuid4()),
                    name=f"held-{index}",
                    image="python:3.12-slim",
                    command=[],
                    workspace_id=warm.workspace_id,
                    stub_id=warm.id,
                    status=ContainerStatus.Running,
                )
            )
    functions = FunctionControlService(isolated_services)
    before = _row_counts(isolated_services, warm.workspace_id)
    invoked = functions.function_invoke(
        FunctionInvokeBody(stub_id=warm.id, invocation=FunctionJsonInvocation(args=[]))
    )
    assert invoked.task_id
    after_warm = _row_counts(isolated_services, warm.workspace_id)
    assert after_warm == (before[0] + 1, before[1])
    with pytest.raises(CapacityLimitReachedError):
        functions.function_invoke(
            FunctionInvokeBody(stub_id=cold.id, invocation=FunctionJsonInvocation(args=[]))
        )
    assert _row_counts(isolated_services, warm.workspace_id) == after_warm


def _past_due(services: ApiServices, workspace_id: str) -> None:
    """A fully provisioned account whose card the provider says was refused.

    Provisioned deliberately: an account holding no subscription is refused too,
    and on a different sentence. Leaving one out here would pass this test
    without the standing it names ever being read.
    """

    user_id = workspace_owner_user_id(services.context, workspace_id)
    with services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.PastDue,
            provider_customer_id="cus_gate",
            provider_subscription_id="sub_gate",
            plan=BillingPlanId.Team,
            subscription_terms_version=SubscriptionTermsVersion.Team,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        session.commit()


def _row_counts(services: ApiServices, workspace_id: str) -> tuple[int, int]:
    with services.context.database.session() as session:
        tasks = session.scalar(select(func.count()).where(TaskTable.workspace_id == workspace_id))
        containers = session.scalar(
            select(func.count()).where(ContainerTable.workspace_id == workspace_id)
        )
    return int(tasks or 0), int(containers or 0)
