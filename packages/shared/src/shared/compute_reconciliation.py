from enum import StrEnum


class ComputeReconciliationKind(StrEnum):
    Provider = "provider"
    Drain = "drain"


COMPUTE_RECONCILIATION_BATCH_SIZE = 3
