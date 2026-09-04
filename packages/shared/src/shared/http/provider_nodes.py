from __future__ import annotations

import re
from datetime import datetime

from pydantic import ConfigDict, Field, field_validator, model_validator

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


class ProviderNodeIdentityRequest(HttpModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    launch_id: str = Field(default="", exclude_if=lambda value: value == "")
    bootstrap_token: str = Field(default="", repr=False, exclude_if=lambda value: value == "")
    node_agent_token: str = Field(default="", repr=False, exclude_if=lambda value: value == "")
    enrollment_request_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    provider: ProviderKind
    region: str = Field(min_length=1, max_length=64)
    provider_instance_id: str = Field(min_length=1, max_length=64)
    identity_proof_url: str = Field(min_length=1, max_length=8192, repr=False)

    @model_validator(mode="after")
    def validate_provider_identity(self) -> ProviderNodeIdentityRequest:
        if self.provider is ProviderKind.Aws:
            valid = re.fullmatch(
                r"(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+", self.region
            ) and re.fullmatch(r"i-[0-9a-f]{8,17}", self.provider_instance_id)
            valid = valid and not (self.launch_id or self.bootstrap_token or self.node_agent_token)
        else:
            valid = (
                self.region in {"ash", "hil", "fsn1", "nbg1", "hel1", "sin"}
                and self.provider_instance_id.isdecimal()
                and int(self.provider_instance_id) > 0
                and self.identity_proof_url == "hetzner-bootstrap"
                and re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                    self.launch_id,
                )
                and re.fullmatch(r"[A-Za-z0-9_-]{43,128}", self.node_agent_token)
                and (
                    not self.bootstrap_token
                    or re.fullmatch(r"[A-Za-z0-9_-]{43,128}", self.bootstrap_token)
                )
            )
        if not valid:
            raise ValueError("invalid provider node identity")
        return self


class ProviderNodeEnrollmentRequest(ProviderNodeIdentityRequest):
    machine_fingerprint: str = Field(min_length=1, max_length=4096, repr=False)
    hostname: str = Field(default="", max_length=255)
    os: str = Field(default="", max_length=128)
    arch: str = Field(default="", max_length=64)
    executor: str = Field(default="", max_length=64)
    capacity: ProviderNodeCapacity = Field(default_factory=ProviderNodeCapacity)
    preflight: list[ComputePreflightCheck] = Field(default_factory=list)
    requested_schedulable: bool = True


class ProviderNodeBootstrapFailureRequest(ProviderNodeIdentityRequest):
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


class ProviderNodeBootstrapPhaseRequest(ProviderNodeIdentityRequest):
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
