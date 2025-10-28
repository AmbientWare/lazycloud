from datetime import datetime, timedelta
from enum import IntEnum, StrEnum


class UsageCollectionInterval(IntEnum):
    """Intervals for usage data collection"""

    MINUTE = 1
    FIVE_MINUTES = 5
    FIFTEEN_MINUTES = 15
    THIRTY_MINUTES = 30
    HOURLY = 60


class UsageRecordType(StrEnum):
    """Type of usage record for aggregation level."""

    MINUTE = "minute"
    FIVE_MINUTES = "five_minutes"
    FIFTEEN_MINUTES = "fifteen_minutes"
    THIRTY_MINUTES = "thirty_minutes"
    HOURLY = "hourly"
    DAILY = "daily"


class UsageRecordStatus(StrEnum):
    """Status of usage record in billing workflow."""

    DRAFT = "draft"
    INCOMPLETE = "incomplete"
    FINALIZED = "finalized"
    REPORTED = "reported"


class UsageCollectionConfig:
    """Configuration for usage data collection intervals"""

    # Primary configuration - change this to adjust collection frequency
    COLLECTION_INTERVAL = UsageCollectionInterval.FIFTEEN_MINUTES

    # Derived values
    INTERVALS_PER_HOUR = 60 // COLLECTION_INTERVAL.value
    INTERVALS_PER_DAY = 24 * INTERVALS_PER_HOUR
    COLLECTION_INTERVAL_TIMEDELTA = timedelta(minutes=COLLECTION_INTERVAL.value)

    # Mapping from collection interval to record type
    INTERVAL_TO_RECORD_TYPE = {
        UsageCollectionInterval.MINUTE: UsageRecordType.MINUTE,
        UsageCollectionInterval.FIVE_MINUTES: UsageRecordType.FIVE_MINUTES,
        UsageCollectionInterval.FIFTEEN_MINUTES: UsageRecordType.FIFTEEN_MINUTES,
        UsageCollectionInterval.THIRTY_MINUTES: UsageRecordType.THIRTY_MINUTES,
        UsageCollectionInterval.HOURLY: UsageRecordType.HOURLY,
    }

    @classmethod
    def get_record_type(cls) -> UsageRecordType:
        """Get the appropriate record type for the current collection interval."""
        return cls.INTERVAL_TO_RECORD_TYPE[cls.COLLECTION_INTERVAL]

    @classmethod
    def get_cron_expression(cls) -> str:
        """Generate cron expression for the collection interval."""
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
        """Get list of minute marks to collect at (e.g., [0, 15, 30, 45])."""
        return list(range(0, 60, cls.COLLECTION_INTERVAL.value))

    @classmethod
    def round_time_to_interval(cls, dt: datetime) -> datetime:
        """Round datetime down to the nearest collection interval."""
        minutes = (
            dt.minute // cls.COLLECTION_INTERVAL.value
        ) * cls.COLLECTION_INTERVAL.value
        return dt.replace(minute=minutes, second=0, microsecond=0)


class MeterNames(StrEnum):
    CPU_USAGE = "CPU Usage"
    MEMORY_USAGE = "Memory Usage"
    STANDARD_STORAGE = "Standard Storage"
    PREMIUM_STORAGE = "Premium Storage"


# Event name for the usage metrics event
USAGE_EVENT_NAME = "lazycloud-usage"

# Metadata field names for each meter to aggregate on
METER_METADATA_FIELDS = {
    MeterNames.CPU_USAGE: "cpu_core_hours",
    MeterNames.MEMORY_USAGE: "memory_gb_hours",
    MeterNames.STANDARD_STORAGE: "s3_gb_hours",
    MeterNames.PREMIUM_STORAGE: "efs_gb_hours",
}

# Storage class name constants
STORAGE_CLASS_S3 = "s3-sc"
STORAGE_CLASS_EFS = "efs-sc"

# Mapping from Kubernetes storage class names to meter display names
STORAGE_CLASS_TO_METER: dict[str, str] = {
    STORAGE_CLASS_S3: MeterNames.STANDARD_STORAGE,
    STORAGE_CLASS_EFS: MeterNames.PREMIUM_STORAGE,
}

# Mapping from storage class to short type names
STORAGE_CLASS_TO_TYPE: dict[str, str] = {
    STORAGE_CLASS_S3: "Standard",
    STORAGE_CLASS_EFS: "High Performance",
}
