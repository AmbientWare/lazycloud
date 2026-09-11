from __future__ import annotations

from pydantic import Field

from shared.http.base import HttpModel
from shared.releases import AgentArtifact

AGENT_RELEASE_GENERATION_HEADER = "X-LazyCloud-Release-Generation"


class AgentReleaseRequest(HttpModel):
    agent_token: str = Field(min_length=1, repr=False)
    generation: int = Field(default=0, ge=0)
    binary_sha256: str = Field(pattern=r"^([0-9a-f]{64})?$")


class AgentReleaseResponse(HttpModel):
    generation: int = Field(ge=0)
    agent: AgentArtifact | None = None
    update_agent: bool = False


__all__ = ["AGENT_RELEASE_GENERATION_HEADER", "AgentReleaseRequest", "AgentReleaseResponse"]
