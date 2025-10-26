from enum import StrEnum


class MeterNames(StrEnum):
    CPU_USAGE = "CPU Usage"
    MEMORY_USAGE = "Memory Usage"
    NORMAL_STORAGE = "Normal Storage"
    HIGH_PERFORMANCE_STORAGE = "High Performance Storage"


# Event name for the usage metrics event
USAGE_EVENT_NAME = "lazycloud-usage"

# Metadata field names for each meter to aggregate on
METER_METADATA_FIELDS = {
    MeterNames.CPU_USAGE: "cpu_core_hours",
    MeterNames.MEMORY_USAGE: "memory_gb_hours",
    MeterNames.NORMAL_STORAGE: "s3_gb_hours",
    MeterNames.HIGH_PERFORMANCE_STORAGE: "efs_gb_hours",
}
