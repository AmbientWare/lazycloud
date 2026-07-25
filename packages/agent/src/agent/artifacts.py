from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import AGENT_NAME, ENV_PREFIX

_ARTIFACT_ARCHITECTURES = ("amd64", "arm64")
_ARTIFACT_VERSION_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
_BINARY_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class AgentArtifactSettings(BaseSettings):
    binary_dir: Path | None = None
    binary_name: str = AGENT_NAME
    artifact_version: str = ""
    artifact_sha256_by_arch: dict[str, str] = Field(default_factory=dict)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_AGENT_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator("binary_name")
    @classmethod
    def validate_binary_name(cls, value: str) -> str:
        return normalize_agent_binary_name(value)

    @model_validator(mode="after")
    def validate_artifact(self) -> AgentArtifactSettings:
        version, digests = normalize_agent_artifact_config(
            self.artifact_version,
            self.artifact_sha256_by_arch,
        )
        if version and self.binary_dir is None:
            raise ValueError("agent artifact binary directory is required")
        self.artifact_version = version
        self.artifact_sha256_by_arch = digests
        return self

    def require_amd64(self) -> tuple[str, str]:
        version = self.artifact_version
        digest = self.artifact_sha256_by_arch.get("amd64", "")
        if not version or not digest:
            raise ValueError("AWS capacity requires an amd64 agent artifact")
        return version, digest


def normalize_agent_binary_name(value: str) -> str:
    name = value.strip()
    if _BINARY_NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(
            "agent binary name must contain only letters, numbers, dot, underscore, or dash"
        )
    return name


def normalize_agent_artifact_config(
    version: str,
    sha256_by_arch: Mapping[str, str],
) -> tuple[str, dict[str, str]]:
    normalized_version = version.strip()
    if normalized_version and _ARTIFACT_VERSION_PATTERN.fullmatch(normalized_version) is None:
        raise ValueError("agent artifact version contains invalid characters")

    digests: dict[str, str] = {}
    for raw_arch, raw_digest in sha256_by_arch.items():
        arch = raw_arch.strip().lower()
        digest = raw_digest.strip().lower()
        if arch not in _ARTIFACT_ARCHITECTURES:
            raise ValueError(f"unsupported agent artifact architecture: {raw_arch}")
        if _SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError(f"agent artifact {arch} digest must be a lowercase SHA-256")
        digests[arch] = digest

    if bool(normalized_version) != bool(digests):
        raise ValueError(
            "agent artifact version and architecture digests must be configured together"
        )
    return normalized_version, digests


__all__ = ["AgentArtifactSettings"]
