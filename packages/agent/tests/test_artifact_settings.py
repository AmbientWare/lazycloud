from __future__ import annotations

from pathlib import Path

import pytest
from agent.artifacts import AgentArtifactSettings
from pydantic import ValidationError


def test_agent_artifact_settings_require_atomic_immutable_configuration() -> None:
    with pytest.raises(ValidationError, match="configured together"):
        AgentArtifactSettings(artifact_version="0.1.0")

    with pytest.raises(ValidationError, match="binary directory is required"):
        AgentArtifactSettings(
            artifact_version="0.1.0",
            artifact_sha256_by_arch={"amd64": "a" * 64},
        )

    with pytest.raises(ValidationError, match="unsupported agent artifact architecture"):
        AgentArtifactSettings(
            binary_dir=Path("/opt/lazycloud/agent"),
            artifact_version="0.1.0",
            artifact_sha256_by_arch={"s390x": "a" * 64},
        )
