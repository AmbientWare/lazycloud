from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_periods import BillingPeriodRepository
from database.repositories.identity import WorkspaceRepository
from pydantic import JsonValue
from shared.billing_accounts import BillingAccountStatus, BillingPlan
from shared.billing_periods import BillingPeriodStatus
from shared.billing_plans import DEFAULT_BILLING_PLANS
from shared.errors import ConflictError
from shared.timestamps import utc_now
from shared.usage import UsageMetric, UsageRecord, UsageUnit
from tests.service_fixtures import owned_workspace, workspace_owner_user_id

from billing import BillingLedgerService, BillingPeriodService, month_bounds, utc_day_bounds


def test_a_closed_period_keeps_its_numbers_when_the_ledger_moves(
    isolated_services: ApiServices,
) -> None:
    """Closing is where a cost stops being a question and becomes what was charged.

    The ledger is recomputed whenever usage changes and priced at whatever the
    catalog says at the time, so an open month moves. A closed one must not: an
    invoice has been built from it, and a figure that drifts afterwards cannot be
    reconciled against what the customer was told.

    The plan's allowance is frozen with it, so re-pricing the plan later does not
    reach back either.
    """

    now = utc_now()
    day = now.date()
    start, end = utc_day_bounds(day)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        user_id = (
            BillingAccountRepository(session)
            .upsert(
                user_id=workspace_owner_user_id(isolated_services.context, workspace_id),
                plan=BillingPlan.Team,
                status=BillingAccountStatus.Active,
            )
            .user_id
        )
        session.commit()

    def meter(seconds: float, marker: str) -> None:
        metadata: dict[str, JsonValue] = {
            "worker_id": "worker-period",
            "window_start_ms": 0,
            "window_end_ms": 1000,
        }
        isolated_services.usage.append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="container",
                resource_id=f"container-{marker}",
                metric=UsageMetric.CpuSeconds,
                quantity=seconds,
                unit=UsageUnit.Seconds,
                labels={"cpu_millicores": "1000", "worker_id": "worker-period"},
                metadata=metadata,
                created_at=start + timedelta(hours=1),
            )
        )

    def price_the_day() -> None:
        overview = isolated_services.usage.billing_overview(
            workspace_id=workspace_id, start=start, end=end, bucket_seconds=86_400
        )
        with isolated_services.context.database.session() as session:
            BillingLedgerService(session).record_day(overview)
            session.commit()

    meter(1_000.0, "first")
    price_the_day()

    with isolated_services.context.database.session() as session:
        closed = BillingPeriodService(session).close_for_month(user_id=user_id, day=day)
        session.commit()

    # More usage lands in a month already settled, and the ledger takes it.
    meter(5_000.0, "late")
    price_the_day()

    with isolated_services.context.database.session() as session:
        periods = BillingPeriodService(session)
        # A settled month refuses to be closed again rather than quietly
        # disagreeing with the invoice already built from it.
        with pytest.raises(ConflictError, match="already been settled"):
            periods.close_for_month(user_id=user_id, day=day)
        reopened = periods.open_for_month(user_id=user_id, day=day)

    team = DEFAULT_BILLING_PLANS.for_plan(BillingPlan.Team)
    assert closed.status is BillingPeriodStatus.Closed
    assert closed.plan is BillingPlan.Team
    # Well under the allowance, so the month bills the subscription and no more.
    assert closed.charged_cost_nanos == team.monthly_price_nanos
    assert reopened.usage_cost_nanos == closed.usage_cost_nanos
    assert reopened.charged_cost_nanos == closed.charged_cost_nanos
    assert month_bounds(day) == (closed.period_start, closed.period_end)


def test_deleting_a_workspace_does_not_erase_what_its_month_owed(
    isolated_services: ApiServices,
) -> None:
    """A workspace is a resource a customer can delete; what it owed is not.

    Ownership is resolved when the day is priced and stored on the line, because
    membership rows go with the workspace. A debt re-derived from live
    membership at invoice time totals to nothing the moment somebody tidies up
    before the month ends — which is a customer deleting a workspace on the 28th
    and paying nothing for the GPU it ran on the 3rd.
    """

    day = utc_now().date()
    start, end = utc_day_bounds(day)
    control = ControlPlaneService(isolated_services.context)
    doomed = owned_workspace(control, "doomed")
    user_id = workspace_owner_user_id(isolated_services.context, doomed.id)

    metadata: dict[str, JsonValue] = {
        "worker_id": "worker-doomed",
        "window_start_ms": 0,
        "window_end_ms": 1000,
    }
    isolated_services.usage.append(
        UsageRecord(
            id=str(uuid4()),
            workspace_id=doomed.id,
            resource_type="container",
            resource_id="container-doomed",
            metric=UsageMetric.CpuSeconds,
            quantity=2_000.0,
            unit=UsageUnit.Seconds,
            labels={"cpu_millicores": "1000", "worker_id": "worker-doomed"},
            metadata=metadata,
            created_at=start + timedelta(hours=1),
        )
    )
    overview = isolated_services.usage.billing_overview(
        workspace_id=doomed.id, start=start, end=end, bucket_seconds=86_400
    )
    with isolated_services.context.database.session() as session:
        BillingLedgerService(session).record_day(overview)
        session.commit()

    with isolated_services.context.database.session() as session:
        owed_before = BillingPeriodRepository(session).usage_cost_for(
            user_id=user_id, period_start=month_bounds(day)[0], period_end=month_bounds(day)[1]
        )
        workspaces = WorkspaceRepository(session)
        record = workspaces.get(doomed.id)
        assert record is not None
        workspaces.mark_deleting(record)
        workspaces.purge_owned_records(doomed.id)
        workspaces.delete_identity_records(doomed.id)
        session.commit()

    with isolated_services.context.database.session() as session:
        owed_after = BillingPeriodRepository(session).usage_cost_for(
            user_id=user_id, period_start=month_bounds(day)[0], period_end=month_bounds(day)[1]
        )

    assert owed_before > 0
    assert owed_after == owed_before


def test_a_month_bills_the_plan_the_account_ends_it_on(
    isolated_services: ApiServices,
) -> None:
    """An upgrade mid-month is billed as the upgrade, not as what it started as.

    A period opens before anyone knows what plan the month will end on, so
    closing has to re-read it. Taking the plan stored when the period opened
    bills a customer who upgraded on the 2nd at the free plan's zero for the
    whole month.
    """

    day = utc_now().date()
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)

    with isolated_services.context.database.session() as session:
        BillingPeriodService(session).open_for_month(user_id=user_id, day=day)
        session.commit()

    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id, plan=BillingPlan.Team, status=BillingAccountStatus.Active
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        closed = BillingPeriodService(session).close_for_month(user_id=user_id, day=day)
        session.commit()

    team = DEFAULT_BILLING_PLANS.for_plan(BillingPlan.Team)
    assert closed.plan is BillingPlan.Team
    assert closed.subscription_cost_nanos == team.monthly_price_nanos
