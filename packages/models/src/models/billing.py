from datetime import datetime, timedelta
from enum import IntEnum, StrEnum


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

USAGE_EVENT_NAME = "lazycloud-usage"

METER_METADATA_FIELDS = {
    MeterNames.CPU_USAGE: "cpu_core_hours",
    MeterNames.MEMORY_USAGE: "memory_gb_hours",
    MeterNames.STANDARD_STORAGE: "standard_gb_hours",
    MeterNames.SHARED_STORAGE: "shared_gb_hours",
    MeterNames.BUILD_MINUTES: "build_minutes",
    MeterNames.PUBLIC_ENDPOINTS: "public_endpoint_hours",
}

STORAGE_CLASS_EBS = "ebs-sc"
STORAGE_CLASS_EFS = "efs-sc"

STORAGE_CLASS_TO_METER: dict[str, str] = {
    STORAGE_CLASS_EBS: MeterNames.STANDARD_STORAGE,
    STORAGE_CLASS_EFS: MeterNames.SHARED_STORAGE,
}

STORAGE_CLASS_TO_TYPE: dict[str, str] = {
    STORAGE_CLASS_EBS: "Standard",
    STORAGE_CLASS_EFS: "Shared",
}
