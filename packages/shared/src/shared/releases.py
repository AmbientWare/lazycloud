from __future__ import annotations

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum

WORKER_IMAGE_DIGEST_PATTERN = r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$"


class AgentArtifact(ContractModel):
    url: str = Field(pattern=r"^https://[^\s]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class RuntimeArtifacts(ContractModel):
    worker_image: str = Field(pattern=WORKER_IMAGE_DIGEST_PATTERN)
    agent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReleaseTarget(ContractModel):
    version: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    worker_image: str = Field(min_length=1)
    agent: AgentArtifact | None = None
    compatible_runtimes: list[RuntimeArtifacts] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )

    def matches(self, worker_image: str, agent_sha256: str) -> bool:
        return worker_image == self.worker_image and (
            self.agent is None or agent_sha256 == self.agent.sha256
        )

    def accepts(self, worker_image: str, agent_sha256: str) -> bool:
        return self.matches(worker_image, agent_sha256) or any(
            runtime.worker_image == worker_image and runtime.agent_sha256 == agent_sha256
            for runtime in self.compatible_runtimes
        )


class ActiveRelease(ContractModel):
    generation: int = Field(gt=0)
    manifest_url: str
    target: ReleaseTarget

    def admits(self, worker_image: str, agent_sha256: str) -> bool:
        return self.target.matches(worker_image, agent_sha256)


class ReleaseMachinePhase(StringEnum):
    Current = "current"
    WaitingForCapacity = "waiting_for_capacity"
    Draining = "draining"
    Updating = "updating"
    Verifying = "verifying"
    PreparingReserve = "preparing_reserve"
    PendingReserve = "pending_reserve"
    Offline = "offline"
    Blocked = "blocked"


class ReleaseMachineStatus(ContractModel):
    machine_id: str
    worker_id: str
    capacity_owner_id: str
    phase: ReleaseMachinePhase
    current: bool
    compatible: bool
    accepting_work: bool
    reason: str = ""


class FleetReleaseStatus(ContractModel):
    release: ActiveRelease
    complete: bool
    machines: list[ReleaseMachineStatus]
    pending_capacity_owners: list[str]


__all__ = [
    "WORKER_IMAGE_DIGEST_PATTERN",
    "ActiveRelease",
    "AgentArtifact",
    "FleetReleaseStatus",
    "ReleaseMachinePhase",
    "ReleaseMachineStatus",
    "ReleaseTarget",
    "RuntimeArtifacts",
]
