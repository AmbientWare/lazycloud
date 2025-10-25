from enum import StrEnum


class MeterNames(StrEnum):
    CPU_USAGE = "CPU Usage"
    MEMORY_USAGE = "Memory Usage"
    NORMAL_STORAGE = "Normal Storage"
    HIGH_PERFORMANCE_STORAGE = "High Performance Storage"


METERS_EVENT_MAP = {
    MeterNames.CPU_USAGE: "cpu_usage",
    MeterNames.MEMORY_USAGE: "memory_usage",
    MeterNames.NORMAL_STORAGE: "s3_storage_usage",
    MeterNames.HIGH_PERFORMANCE_STORAGE: "efs_storage_usage",
}
