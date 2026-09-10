from __future__ import annotations

from pydantic import Field

from shared.contracts import ContractModel


class AgentArtifact(ContractModel):
    url: str = Field(pattern=r"^https://[^\s]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class ReleaseTarget(ContractModel):
    version: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    worker_image: str = Field(min_length=1)
    agent: AgentArtifact | None = None


class ActiveRelease(ContractModel):
    generation: int = Field(gt=0)
    manifest_url: str
    target: ReleaseTarget

    def admits(self, worker_image: str, agent_sha256: str) -> bool:
        return worker_image == self.target.worker_image and (
            self.target.agent is None or agent_sha256 == self.target.agent.sha256
        )


__all__ = [
    "ActiveRelease",
    "AgentArtifact",
    "ReleaseTarget",
]
