from __future__ import annotations

import re
from urllib.parse import urlparse

from agent.artifacts import AgentArtifactSettings
from provider_aws import AwsManagedPoolArtifacts
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

_AMI_PATTERN = re.compile(r"ami-[0-9a-f]{8,17}")
_AWS_PRINCIPAL_PATTERN = re.compile(
    r"arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:"
    r"(root|role/[A-Za-z0-9+=,.@_/-]+|user/[A-Za-z0-9+=,.@_/-]+)"
)
_AWS_REGION_PATTERN = re.compile(r"(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+")


class AwsAccountConnectionSettings(BaseSettings):
    enabled: bool = False
    template_url: str = ""
    control_principal_arn: str = ""
    external_id_bytes: int = Field(default=48, ge=32, le=128)
    draft_ttl_seconds: int = Field(default=24 * 60 * 60, gt=0)
    cleanup_tombstone_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, gt=0)
    cleanup_timeout_seconds: int = Field(default=2 * 60 * 60, gt=0)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_AWS_CONNECTION_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator("template_url", "control_principal_arn")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_enabled_configuration(self) -> AwsAccountConnectionSettings:
        if not self.enabled:
            return self
        missing = [
            name
            for name, value in (
                ("connection template URL", self.template_url),
                ("control principal ARN", self.control_principal_arn),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "AWS account connection configuration is incomplete: missing " + ", ".join(missing)
            )
        if _AWS_PRINCIPAL_PATTERN.fullmatch(self.control_principal_arn) is None:
            raise ValueError("AWS account connection control principal ARN is invalid")
        return self


class AwsCapacitySettings(BaseSettings):
    worker_image_digest: str = ""
    agent_artifact_url: str = ""
    cpu_ami_ids: dict[str, str] = Field(default_factory=dict)
    gpu_ami_ids: dict[str, str] = Field(default_factory=dict)
    instance_hourly_micros: dict[str, int] = Field(default_factory=dict)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_AWS_CAPACITY_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator("worker_image_digest")
    @classmethod
    def normalize_worker_image_digest(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("agent_artifact_url")
    @classmethod
    def validate_agent_artifact_url(cls, value: str) -> str:
        url = value.strip()
        if not url:
            return url
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError(
                "AWS capacity agent artifact URL must be an HTTPS URL without credentials"
            )
        return url

    @field_validator("cpu_ami_ids", "gpu_ami_ids")
    @classmethod
    def normalize_ami_catalog(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_region, raw_ami_id in value.items():
            region = raw_region.strip().lower()
            ami_id = raw_ami_id.strip().lower()
            if _AWS_REGION_PATTERN.fullmatch(region) is None:
                raise ValueError("AWS AMI catalog has invalid region")
            if _AMI_PATTERN.fullmatch(ami_id) is None:
                raise ValueError("AWS AMI catalog has invalid AMI ID")
            normalized[region] = ami_id
        return normalized

    @field_validator("instance_hourly_micros")
    @classmethod
    def normalize_instance_prices(cls, value: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for raw_instance_type, hourly_micros in value.items():
            instance_type = raw_instance_type.strip().lower()
            if not instance_type:
                raise ValueError("AWS capacity instance price keys cannot be empty")
            if hourly_micros < 0:
                raise ValueError("AWS capacity instance prices cannot be negative")
            normalized[instance_type] = hourly_micros
        return normalized

    @model_validator(mode="after")
    def validate_atomic_configuration(self) -> AwsCapacitySettings:
        configured = any(
            (
                self.worker_image_digest,
                self.agent_artifact_url,
                self.cpu_ami_ids,
                self.gpu_ami_ids,
                self.instance_hourly_micros,
            )
        )
        if not configured:
            return self
        missing = [
            name
            for name, value in (
                ("worker image digest", self.worker_image_digest),
                ("agent artifact URL", self.agent_artifact_url),
                ("regional CPU AMI catalog", self.cpu_ami_ids),
                ("regional GPU AMI catalog", self.gpu_ami_ids),
                ("instance price estimates", self.instance_hourly_micros),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "AWS capacity configuration is incomplete: missing " + ", ".join(missing)
            )
        return self

    def artifacts_by_region(
        self,
        agent_artifact: AgentArtifactSettings,
    ) -> dict[str, AwsManagedPoolArtifacts]:
        agent_version, agent_sha256 = agent_artifact.require_amd64()
        if not self.worker_image_digest:
            raise ValueError("AWS capacity is not configured")
        regions = sorted(self.cpu_ami_ids.keys() | self.gpu_ami_ids.keys())
        if not regions or not self.instance_hourly_micros:
            raise ValueError("AWS capacity is not configured")
        return {
            region: AwsManagedPoolArtifacts(
                agent_version=agent_version,
                agent_sha256=agent_sha256,
                worker_image_digest=self.worker_image_digest,
                cpu_ami_id=self.cpu_ami_ids.get(region),
                gpu_ami_id=self.gpu_ami_ids.get(region),
            )
            for region in regions
        }


class AwsCapacityReconciliationSettings(BaseSettings):
    interval_seconds: float = Field(default=30, gt=0, le=3600)
    limit: int = Field(default=25, ge=1, le=100)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_AWS_CAPACITY_RECONCILIATION_",
        env_file=".env",
        extra="ignore",
    )


__all__ = [
    "AwsAccountConnectionSettings",
    "AwsCapacityReconciliationSettings",
    "AwsCapacitySettings",
]
