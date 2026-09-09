import pytest
from api.server.services import ApiServices
from billing.rate_publication import publish_metered_rate_history
from database.repositories.billing_credits import BillingCreditRepository
from shared.billing_credits import CreditGrant, CreditKind, CreditScope
from shared.billing_rate_card import METERED_RATES_EFFECTIVE_AT
from shared.timestamps import utc_now
from tests.service_fixtures import isolated_services, workspace_owner_user_id

__all__ = ["isolated_services"]


@pytest.fixture
def funded_execution_account(isolated_services: ApiServices) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        now = utc_now()
        publish_metered_rate_history(session, effective_at=METERED_RATES_EFFECTIVE_AT)
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=now)
        credits.complete_cutover(user_id=user_id, at=now)
        credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "fixture:execution-credit",
                CreditKind.Purchased,
                CreditScope.AllMetered,
                100_000_000_000,
                now,
            ),
        )
