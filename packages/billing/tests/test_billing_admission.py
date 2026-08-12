from __future__ import annotations

from datetime import UTC, datetime

import pytest
from api.server.services import ApiServices
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_ledger import BillingLedgerRepository
from database.tables.orchestration import ContainerTable
from shared.billing import BillableMetric
from shared.billing_accounts import BillingAccountStatus, BillingPlan
from shared.billing_ledger import BillingLedgerEntry
from shared.billing_plans import DEFAULT_BILLING_PLANS
from shared.errors import PaymentRequiredError
from shared.timestamps import utc_now
from shared.usage import UsageUnit
from sqlalchemy import func, select
from tests.service_fixtures import workspace_owner_user_id

FUNCTION_IMAGE = "python:3.12-slim"


def test_an_account_behind_on_payment_cannot_start_work_and_leaves_no_container(
    isolated_services: ApiServices,
) -> None:
    """Refused before the container exists, not after.

    A check that ran once the row was written would have to undo it, and the
    version of that which runs after a crash never happens at all — leaving a
    pending container nobody scheduled and nobody cleans up. So the refusal is
    taken inside the transaction that would have created it, and the proof is
    that the table is untouched.

    """

    workspace_id, _ = _account(isolated_services, BillingAccountStatus.PastDue)

    before = _container_count(isolated_services, workspace_id)
    with pytest.raises(PaymentRequiredError, match="did not go through"):
        isolated_services.containers.run(
            "past-due-container",
            FUNCTION_IMAGE,
            ["python", "-m", "runner.function"],
            workspace_id=workspace_id,
        )
    assert _container_count(isolated_services, workspace_id) == before


def test_an_account_with_a_card_may_run_past_its_included_usage(
    isolated_services: ApiServices,
) -> None:
    """Overage is what a paid plan is for, not a reason to stop.

    The gate exists to refuse accounts that cannot pay. One that has given a way
    to charge it is billed for what it runs at the end of the month like
    everything else, and stopping it at the allowance would be refusing a
    customer their own compute over money they have agreed to pay.
    """

    workspace_id, user_id = _account(isolated_services, BillingAccountStatus.Active, card=True)
    _spend(isolated_services, workspace_id=workspace_id, user_id=user_id, over_allowance=True)

    isolated_services.containers.run(
        "paying-container",
        FUNCTION_IMAGE,
        ["python", "-m", "runner.function"],
        workspace_id=workspace_id,
    )
    assert _container_count(isolated_services, workspace_id) == 1


def test_an_account_with_no_way_to_pay_stops_when_its_free_usage_is_spent(
    isolated_services: ApiServices,
) -> None:
    """Nothing here can be collected, so the allowance is the whole of the credit.

    Left ungated this account runs up a debt the close turns into an invoice
    nobody can charge — the platform having already paid for the compute.
    """

    workspace_id, user_id = _account(isolated_services, BillingAccountStatus.Active)
    _spend(isolated_services, workspace_id=workspace_id, user_id=user_id, over_allowance=True)

    before = _container_count(isolated_services, workspace_id)
    with pytest.raises(PaymentRequiredError, match="included usage is spent"):
        isolated_services.containers.run(
            "spent-container",
            FUNCTION_IMAGE,
            ["python", "-m", "runner.function"],
            workspace_id=workspace_id,
        )
    assert _container_count(isolated_services, workspace_id) == before


def _account(
    services: ApiServices, status: BillingAccountStatus, *, card: bool = False
) -> tuple[str, str]:
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(services.context, workspace_id)
    with services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            plan=BillingPlan.Free,
            status=status,
            provider_customer_id="cus_gate" if card else "",
        )
        session.commit()
    return workspace_id, user_id


def _spend(services: ApiServices, *, workspace_id: str, user_id: str, over_allowance: bool) -> None:
    """Put priced usage on the ledger for this month, the way the daily job does."""

    included = DEFAULT_BILLING_PLANS.for_plan(BillingPlan.Free).included_cost_nanos
    today = utc_now().date()
    with services.context.database.session() as session:
        BillingLedgerRepository(session).record_day(
            workspace_id=workspace_id,
            day=today,
            entries=(
                BillingLedgerEntry(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    day=today,
                    metric=BillableMetric.CpuSeconds,
                    variant="",
                    effective_date=datetime(2026, 8, 11, tzinfo=UTC).date(),
                    quantity=1.0,
                    unit=UsageUnit.Seconds,
                    price_per_unit_nanos=1,
                    cost_nanos=included + 1 if over_allowance else included // 4,
                    currency="USD",
                ),
            ),
        )
        session.commit()


def _container_count(services: ApiServices, workspace_id: str) -> int:
    with services.context.database.session() as session:
        return int(
            session.scalar(select(func.count()).where(ContainerTable.workspace_id == workspace_id))
            or 0
        )
