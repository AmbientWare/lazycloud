from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import AGENT_NAME, ENV_PREFIX

_ARTIFACT_ARCHITECTURES = ("amd64", "arm64")
_ARTIFACT_VERSION_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
_BINARY_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class AgentBinaryEnvironmentSettings(BaseSettings):
    """Where a deployment keeps the agent binaries it serves.

    The directory is a local mount and the name is the file layout inside it, so
    both belong to the deployment. Which version that directory holds, and what
    it must hash to, are facts of the release that filled it and are resolved
    from the release manifest instead of being set here.
    """

    binary_dir: Path | None = None
    binary_name: str = AGENT_NAME

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_AGENT_",
        extra="ignore",
    )

    @field_validator("binary_name")
    @classmethod
    def validate_binary_name(cls, value: str) -> str:
        return normalize_agent_binary_name(value)


class AgentBinarySettings(BaseModel):
    """The agent artifacts a deployment serves: its local mount and the release in it."""

    model_config = ConfigDict(frozen=True)

    binary_dir: Path | None = None
    binary_name: str = AGENT_NAME
    binary_version: str = ""
    binary_sha256_by_arch: dict[str, str] = Field(default_factory=dict)

    @field_validator("binary_name")
    @classmethod
    def validate_binary_name(cls, value: str) -> str:
        return normalize_agent_binary_name(value)

    @field_validator("binary_version")
    @classmethod
    def validate_binary_version(cls, value: str) -> str:
        return normalize_agent_artifact_version(value)

    @field_validator("binary_sha256_by_arch")
    @classmethod
    def validate_binary_digests(cls, value: dict[str, str]) -> dict[str, str]:
        return normalize_agent_artifact_digests(value)

    @model_validator(mode="after")
    def validate_artifact(self) -> AgentBinarySettings:
        if bool(self.binary_version) != bool(self.binary_sha256_by_arch):
            raise ValueError(
                "agent artifact version and architecture digests must be configured together"
            )
        if self.binary_version and self.binary_dir is None:
            raise ValueError("agent artifact binary directory is required")
        return self

    def require_amd64(self) -> tuple[str, str]:
        version = self.binary_version
        digest = self.binary_sha256_by_arch.get("amd64", "")
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


def normalize_agent_artifact_version(value: str) -> str:
    version = value.strip()
    if version and _ARTIFACT_VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("agent artifact version contains invalid characters")
    return version


def normalize_agent_artifact_digests(sha256_by_arch: Mapping[str, str]) -> dict[str, str]:
    digests: dict[str, str] = {}
    for raw_arch, raw_digest in sha256_by_arch.items():
        arch = raw_arch.strip().lower()
        digest = raw_digest.strip().lower()
        if arch not in _ARTIFACT_ARCHITECTURES:
            raise ValueError(f"unsupported agent artifact architecture: {raw_arch}")
        if _SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError(f"agent artifact {arch} digest must be a lowercase SHA-256")
        digests[arch] = digest
    return digests


def normalize_agent_artifact_config(
    version: str,
    sha256_by_arch: Mapping[str, str],
) -> tuple[str, dict[str, str]]:
    normalized_version = normalize_agent_artifact_version(version)
    digests = normalize_agent_artifact_digests(sha256_by_arch)
    if bool(normalized_version) != bool(digests):
        raise ValueError(
            "agent artifact version and architecture digests must be configured together"
        )
    return normalized_version, digests


__all__ = ["AgentBinaryEnvironmentSettings", "AgentBinarySettings"]
