from shared.enums import StringEnum


class ComputeReconciliationKind(StringEnum):
    Provider = "provider"
    Drain = "drain"


COMPUTE_RECONCILIATION_BATCH_SIZE = 3
