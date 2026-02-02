from enum import StrEnum


class DailyUsageStatus(StrEnum):
    COLLECTING = "collecting"
    BILLED = "billed"


class BreakdownType(StrEnum):
    COMPUTE = "compute"
    STORAGE = "storage"
    NETWORK = "network"
    BUILD = "build"
