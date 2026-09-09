from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from api.server.services import ApiServices
from billing.preferences import BillingPreferencesService
from database.repositories.billing_ledger import BillingLedgerRepository
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.observability import UsageRepository
from shared.http.billing_preferences import BillingPreferences
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from tests.service_fixtures import unfunded_billing_account


def test_monthly_budget_counts_recorded_usage_across_rollover_and_saved_edits(
    postgres_services: ApiServices,
) -> None:
    start = datetime(2026, 9, 30, 23, 59, 50, tzinfo=UTC)
    end = start + timedelta(seconds=20)
    user_id, workspace_id = unfunded_billing_account(
        postgres_services.context,
        period_started_at=start,
        period_ended_at=start + timedelta(days=30),
    )
    record = UsageRecord(
        id=str(uuid4()),
        workspace_id=workspace_id,
        resource_type="volume",
        resource_id="monthly-budget",
        metric=UsageMetric.PersistentVolumeByteSeconds,
        quantity=30,
        unit=UsageUnit.ByteSeconds,
        metadata={
            METERING_WINDOW_STARTED_AT_METADATA_KEY: start.isoformat(),
            METERING_WINDOW_ENDED_AT_METADATA_KEY: end.isoformat(),
        },
        created_at=end,
    )
    with postgres_services.context.database.session() as session:
        PlatformRateRepository(session).publish(
            pricing_version="monthly-budget",
            effective_at=start,
            nanos_per_egress_byte=Decimal(0),
            nanos_per_volume_byte_second=Decimal(1),
        )
        UsageRepository(session).append_storage(record)
        BillingLedgerRepository(session).price_record(record)
        preferences = BillingPreferencesService(session)
        preferences.set(
            user_id=user_id, preferences=BillingPreferences(monthly_usage_limit_nanos=20)
        )
        september = preferences.usage_budget(user_id=user_id, at=start)
        october = preferences.usage_budget(user_id=user_id, at=end)
        assert september.spent_nanos == october.spent_nanos == 15
        assert september.available_nanos == october.available_nanos == 5
        preferences.set(
            user_id=user_id, preferences=BillingPreferences(monthly_usage_limit_nanos=10)
        )
        assert preferences.usage_budget(user_id=user_id, at=end).available_nanos == 0
        preferences.set(user_id=user_id, preferences=BillingPreferences())
        assert preferences.usage_budget(user_id=user_id, at=end).available_nanos is None
