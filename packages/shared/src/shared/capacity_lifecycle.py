from datetime import datetime

from pydantic import ConfigDict, Field

from shared.contracts import ContractModel
from shared.enums import StringEnum


class CapacitySleepMode(StringEnum):
    Stop = "stop"
    Hibernate = "hibernate"


class CapacityImageEvidence(StringEnum):
    Unknown = "unknown"
    Saved = "saved"
    Failed = "failed"
    Unavailable = "unavailable"


class CapacitySleepReason(StringEnum):
    SaveCompleted = "save_completed"
    SaveFailed = "save_failed"
    Unsupported = "unsupported"
    PlainStop = "plain_stop"
    EvidenceMissing = "evidence_missing"
    EvidenceExpired = "evidence_expired"
    ExternalChange = "external_change"
    LegacyUnknown = "legacy_unknown"
    ProviderRejected = "provider_rejected"
    ForcedStop = "forced_stop"
    SaveAborted = "save_aborted"


class CapacityRestoreOutcome(StringEnum):
    Unknown = "unknown"
    ColdBoot = "cold_boot"
    MemoryRestored = "memory_restored"


class CapacityActivationKind(StringEnum):
    Provision = "provision"
    Boot = "boot"
    Resume = "resume"


class CapacitySleepRequest(ContractModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    boot_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    mode: CapacitySleepMode
    requested_at: datetime


class CapacitySleepObservation(ContractModel):
    model_config = ConfigDict(frozen=True)

    attempt_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    boot_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    suspended_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
