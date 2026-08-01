from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from shared.compute_enrollment import (
    ComputePreflightCheck,
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
)
from shared.http.base import HttpModel
from shared.provider_config import ProviderKind


class ProviderNodeCapacity(HttpModel):
    cpu_count: int = Field(default=0, ge=0)
    cpu_millicores: int = Field(default=0, ge=0)
    memory_mb: int = Field(default=0, ge=0)
    gpu: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    gpu_count: int = Field(default=0, ge=0)


class ProviderNodeEnrollmentRequest(HttpModel):
    enrollment_request_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    provider: ProviderKind
    region: str = Field(pattern=r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
    provider_instance_id: str = Field(pattern=r"^i-[0-9a-f]{8,17}$")
    identity_proof_url: str = Field(min_length=1, max_length=8192, repr=False)
    machine_fingerprint: str = Field(min_length=1, max_length=4096, repr=False)
    hostname: str = Field(default="", max_length=255)
    os: str = Field(default="", max_length=128)
    arch: str = Field(default="", max_length=64)
    executor: str = Field(default="", max_length=64)
    capacity: ProviderNodeCapacity = Field(default_factory=ProviderNodeCapacity)
    preflight: list[ComputePreflightCheck] = Field(default_factory=list)
    requested_schedulable: bool = True


class ProviderNodeBootstrapFailureRequest(HttpModel):
    enrollment_request_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    provider: ProviderKind
    region: str = Field(pattern=r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
    provider_instance_id: str = Field(pattern=r"^i-[0-9a-f]{8,17}$")
    identity_proof_url: str = Field(min_length=1, max_length=8192, repr=False)
    failure_reason: MachineBootstrapFailureReason
    # Sensitive: a bootstrap log can carry a credential. Persisted only after
    # the node's identity proof verifies; never logged at INFO.
    diagnostic_excerpt: str = Field(default="", max_length=8192, repr=False)


_REPORTABLE_BOOTSTRAP_PHASES = frozenset(
    {
        MachineBootstrapPhase.Provisioning,
        MachineBootstrapPhase.Booting,
        MachineBootstrapPhase.Joining,
    }
)


class ProviderNodeBootstrapPhaseRequest(HttpModel):
    enrollment_request_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    provider: ProviderKind
    region: str = Field(pattern=r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
    provider_instance_id: str = Field(pattern=r"^i-[0-9a-f]{8,17}$")
    identity_proof_url: str = Field(min_length=1, max_length=8192, repr=False)
    phase: MachineBootstrapPhase

    @field_validator("phase")
    @classmethod
    def _reportable_phase(cls, value: MachineBootstrapPhase) -> MachineBootstrapPhase:
        if value not in _REPORTABLE_BOOTSTRAP_PHASES:
            raise ValueError(
                "bootstrap phase reports accept only in-progress phases; "
                "failures use the bootstrap failure report"
            )
        return value


class ProviderNodeBootstrapFailureResponse(HttpModel):
    provider_instance_id: str
    phase: MachineBootstrapPhase
    failure_reason: MachineBootstrapFailureReason | None = None
    observed_at: datetime


__all__ = [
    "ProviderNodeBootstrapFailureRequest",
    "ProviderNodeBootstrapFailureResponse",
    "ProviderNodeBootstrapPhaseRequest",
    "ProviderNodeCapacity",
    "ProviderNodeEnrollmentRequest",
]
