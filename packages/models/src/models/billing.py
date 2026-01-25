from datetime import datetime, timedelta
from enum import IntEnum, StrEnum

from models.storage import STORAGE_CLASS_EBS, STORAGE_CLASS_EFS


class UsageCollectionInterval(IntEnum):
    MINUTE = 1
    FIVE_MINUTES = 5
    FIFTEEN_MINUTES = 15
    THIRTY_MINUTES = 30
    HOURLY = 60


class UsageCollectionConfig:
    COLLECTION_INTERVAL = UsageCollectionInterval.FIFTEEN_MINUTES

    INTERVALS_PER_HOUR = 60 // COLLECTION_INTERVAL.value
    INTERVALS_PER_DAY = 24 * INTERVALS_PER_HOUR
    COLLECTION_INTERVAL_TIMEDELTA = timedelta(minutes=COLLECTION_INTERVAL.value)

    @classmethod
    def get_cron_expression(cls) -> str:
        if cls.COLLECTION_INTERVAL == UsageCollectionInterval.HOURLY:
            return "15 * * * *"
        elif cls.COLLECTION_INTERVAL == UsageCollectionInterval.FIFTEEN_MINUTES:
            return "*/15 * * * *"
        elif cls.COLLECTION_INTERVAL == UsageCollectionInterval.THIRTY_MINUTES:
            return "*/30 * * * *"
        elif cls.COLLECTION_INTERVAL == UsageCollectionInterval.MINUTE:
            return f"*/{cls.COLLECTION_INTERVAL.value} * * * *"
        elif cls.COLLECTION_INTERVAL == UsageCollectionInterval.FIVE_MINUTES:
            return "*/5 * * * *"

    @classmethod
    def get_minute_marks(cls) -> list[int]:
        return list(range(0, 60, cls.COLLECTION_INTERVAL.value))

    @classmethod
    def round_time_to_interval(cls, dt: datetime) -> datetime:
        minutes = (
            dt.minute // cls.COLLECTION_INTERVAL.value
        ) * cls.COLLECTION_INTERVAL.value
        return dt.replace(minute=minutes, second=0, microsecond=0)


class MeterNames(StrEnum):
    CPU_USAGE = "CPU Usage"
    MEMORY_USAGE = "Memory Usage"
    STANDARD_STORAGE = "Standard Storage"
    SHARED_STORAGE = "Shared Storage"
    BUILD_MINUTES = "Build Minutes"
    PUBLIC_ENDPOINTS = "Public Endpoints"


SECONDS_PER_HOUR = 3600
CENTS_PER_DOLLAR = 100

USAGE_EVENT_NAME = "lazycloud-usage"

# Meter prices in cents per unit
# - CPU: cents per core-hour
# - Memory: cents per GB-hour
# - Storage: cents per GB-hour
# - Build: cents per minute
# - Endpoints: cents per endpoint-hour
METER_PRICES_CENTS: dict[MeterNames, float] = {
    MeterNames.CPU_USAGE: 4.0,  # $0.04 per core-hour
    MeterNames.MEMORY_USAGE: 0.8,  # $0.008 per GB-hour
    MeterNames.STANDARD_STORAGE: 0.015,  # $0.00015 per GB-hour (~$0.11/GB-month)
    MeterNames.SHARED_STORAGE: 0.06,  # $0.0006 per GB-hour (~$0.43/GB-month)
    MeterNames.BUILD_MINUTES: 4.0,  # $0.04 per minute
    MeterNames.PUBLIC_ENDPOINTS: 0.07,  # $0.0007 per endpoint-hour (~$0.50/month)
}


class UsageUnits:
    """Centralized utility for usage unit conversions and cost calculations.

    All prices are stored in cents. All usage is converted to hours (except build minutes).
    This class provides a single source of truth for conversions used by both
    Polar billing and CLI display.
    """

    @staticmethod
    def seconds_to_hours(seconds: float) -> float:
        """Convert seconds to hours."""
        return seconds / SECONDS_PER_HOUR

    @staticmethod
    def cents_to_dollars(cents: float) -> float:
        """Convert cents to dollars."""
        return cents / CENTS_PER_DOLLAR

    @staticmethod
    def get_price_cents(meter: MeterNames) -> float:
        """Get the price in cents for a meter."""
        return METER_PRICES_CENTS[meter]

    @staticmethod
    def get_price_dollars(meter: MeterNames) -> float:
        """Get the price in dollars for a meter."""
        return UsageUnits.cents_to_dollars(METER_PRICES_CENTS[meter])

    @staticmethod
    def calculate_cost_dollars(usage: float, meter: MeterNames) -> float:
        """Calculate cost in dollars for given usage.

        Args:
            usage: Usage amount in the meter's native unit (hours for most, minutes for build)
            meter: The meter type

        Returns:
            Cost in dollars
        """
        price_dollars = UsageUnits.get_price_dollars(meter)
        return usage * price_dollars

    @staticmethod
    def format_size_gb(size_gb: float) -> str:
        """Format size in GB to a human-readable string."""
        if size_gb >= 1.0:
            return f"{size_gb:.1f} GB"
        else:
            size_mb = size_gb * 1024
            return f"{size_mb:.0f} MB"


METER_METADATA_FIELDS = {
    MeterNames.CPU_USAGE: "cpu_core_hours",
    MeterNames.MEMORY_USAGE: "memory_gb_hours",
    MeterNames.STANDARD_STORAGE: "standard_gb_hours",
    MeterNames.SHARED_STORAGE: "shared_gb_hours",
    MeterNames.BUILD_MINUTES: "build_minutes",
    MeterNames.PUBLIC_ENDPOINTS: "public_endpoint_hours",
}

STORAGE_CLASS_TO_METER: dict[str, MeterNames] = {
    STORAGE_CLASS_EBS: MeterNames.STANDARD_STORAGE,
    STORAGE_CLASS_EFS: MeterNames.SHARED_STORAGE,
}
