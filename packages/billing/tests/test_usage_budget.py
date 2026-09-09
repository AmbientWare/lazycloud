from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from billing.funding import BillingFundingService
from billing.rate_publication import publish_metered_rate_history
from database.repositories.billing_credits import BillingCreditRepository
from shared.billing_credits import CreditGrant, CreditKind, CreditScope
from shared.billing_quotes import ContainerShape
from shared.billing_rate_card import METERED_RATES_EFFECTIVE_AT
from shared.errors import PaymentRequiredError
from shared.http.billing_preferences import BillingPreferences
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner
from tests.service_fixtures import workspace_owner_user_id


def test_monthly_cap_serializes_starts_and_covers_rollover_and_saved_edits(
    postgres_services: ApiServices,
) -> None:
    now = utc_now()
    start = datetime(2026, 9, 30, 23, 59, 50, tzinfo=UTC)
    next_month = start + timedelta(seconds=20)
    shape = ContainerShape(UsageBillingOwner.PlatformFleet, "", 17000, 4096, 0)
    with postgres_services.context.database.session() as session:
        workspace_id = postgres_services.context.default_workspace_id(session)
        user_id = workspace_owner_user_id(postgres_services.context, workspace_id)
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        credits = BillingCreditRepository(session)
        credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "payment:monthly-budget",
                CreditKind.Purchased,
                CreditScope.AllMetered,
                100_000_000_000,
                now,
            ),
        )
        funding = BillingFundingService(session)
        sizing_id = str(uuid4())
        funding.reserve_pending(
            container_id=sizing_id, workspace_id=workspace_id, candidate_shapes=(shape,), now=start
        )
        first_month = funding.usage_budget(user_id=user_id, at=start).held_nanos
        full_window = funding.usage_budget(user_id=user_id, at=next_month).held_nanos
        assert 0 < first_month < full_window
        funding.cancel_pending(container_id=sizing_id, now=start)
        funding.set_preferences(
            user_id=user_id,
            preferences=BillingPreferences(monthly_usage_limit_nanos=first_month + 1),
        )
    with (
        pytest.raises(PaymentRequiredError, match="monthly usage limit"),
        postgres_services.context.database.session() as session,
    ):
        BillingFundingService(session).reserve_pending(
            container_id=str(uuid4()),
            workspace_id=workspace_id,
            candidate_shapes=(shape,),
            now=start,
        )
    with postgres_services.context.database.session() as session:
        BillingFundingService(session).set_preferences(
            user_id=user_id,
            preferences=BillingPreferences(monthly_usage_limit_nanos=full_window),
        )
    barrier = Barrier(2)

    def reserve() -> bool:
        barrier.wait(timeout=5)
        try:
            with postgres_services.context.database.session() as session:
                BillingFundingService(session).reserve_pending(
                    container_id=str(uuid4()),
                    workspace_id=workspace_id,
                    candidate_shapes=(shape,),
                    now=start,
                )
            return True
        except PaymentRequiredError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(reserve) for _ in range(2)]
        assert sorted(attempt.result(timeout=15) for attempt in attempts) == [False, True]
    with postgres_services.context.database.session() as session:
        funding = BillingFundingService(session)
        assert funding.usage_budget(user_id=user_id, at=next_month).held_nanos == full_window
        funding.set_preferences(
            user_id=user_id,
            preferences=BillingPreferences(monthly_usage_limit_nanos=0),
        )
    with (
        pytest.raises(PaymentRequiredError, match="monthly usage limit"),
        postgres_services.context.database.session() as session,
    ):
        funding = BillingFundingService(session)
        assert funding.get_preferences(user_id=user_id).monthly_usage_limit_nanos == 0
        funding.reserve_pending(
            container_id=str(uuid4()),
            workspace_id=workspace_id,
            candidate_shapes=(shape,),
            now=next_month,
        )
