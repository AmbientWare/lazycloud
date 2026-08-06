from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlparse

from agent.binary import AgentBinarySettings
from provider_aws import AwsManagedPoolBinaries
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

_AMI_PATTERN = re.compile(r"ami-[0-9a-f]{8,17}")
_AWS_PRINCIPAL_PATTERN = re.compile(
    r"arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:"
    r"(root|role/[A-Za-z0-9+=,.@_/-]+|user/[A-Za-z0-9+=,.@_/-]+)"
)
_AWS_REGION_PATTERN = re.compile(r"(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+")
_MANIFEST_URL_ENV = f"{ENV_PREFIX}_RELEASE_MANIFEST_URL"
_GPU_AMI_IDS_ENV = f"{ENV_PREFIX}_AWS_CAPACITY_GPU_AMI_IDS"
_INSTANCE_PRICES_ENV = f"{ENV_PREFIX}_AWS_CAPACITY_INSTANCE_HOURLY_MICROS"
_CONTROL_PRINCIPAL_ENV = f"{ENV_PREFIX}_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN"


def normalize_ami_catalog(value: Mapping[str, str]) -> dict[str, str]:
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


def normalize_instance_prices(value: Mapping[str, int]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for raw_instance_type, hourly_micros in value.items():
        instance_type = raw_instance_type.strip().lower()
        if not instance_type:
            raise ValueError("AWS capacity instance price keys cannot be empty")
        if hourly_micros < 0:
            raise ValueError("AWS capacity instance prices cannot be negative")
        normalized[instance_type] = hourly_micros
    return normalized


class AwsAccountConnectionEnvironmentSettings(BaseSettings):
    """The connected-AWS choices a deployment authors.

    The customer authorization template is published by a release, not chosen
    here, so its URL is resolved from the release manifest.
    """

    enabled: bool = False
    control_principal_arn: str = ""
    external_id_bytes: int = 48
    draft_ttl_seconds: int = 24 * 60 * 60
    cleanup_tombstone_ttl_seconds: int = 7 * 24 * 60 * 60
    cleanup_timeout_seconds: int = 2 * 60 * 60

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_AWS_CONNECTION_",
        extra="ignore",
    )


class AwsAccountConnectionSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    template_url: str = ""
    control_principal_arn: str = ""
    external_id_bytes: int = Field(default=48, ge=32, le=128)
    draft_ttl_seconds: int = Field(default=24 * 60 * 60, gt=0)
    cleanup_tombstone_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, gt=0)
    cleanup_timeout_seconds: int = Field(default=2 * 60 * 60, gt=0)

    @field_validator("template_url", "control_principal_arn")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.template_url and self.control_principal_arn)

    @model_validator(mode="after")
    def validate_enabled_configuration(self) -> AwsAccountConnectionSettings:
        """Connected AWS is opted into explicitly, and opting in requires its configuration.

        ``enabled`` is a deployment declaration, not a runtime switch: a deployment that
        leaves it false has no connected AWS at all, and one that sets it true must have
        both values or fail here rather than degrade. Publishing a customer authorization
        template with no control principal would trust nothing, so the two are one unit,
        even though they now arrive from different places.
        """

        if self.control_principal_arn and (
            _AWS_PRINCIPAL_PATTERN.fullmatch(self.control_principal_arn) is None
        ):
            raise ValueError("AWS account connection control principal ARN is invalid")
        if not self.enabled:
            return self
        missing: list[str] = []
        if not self.template_url:
            missing.append(
                f"connection template URL (published by the release at {_MANIFEST_URL_ENV})"
            )
        if not self.control_principal_arn:
            missing.append(
                f"control principal ARN ({_CONTROL_PRINCIPAL_ENV}, which "
                "deploy/connected-aws/bootstrap.py prints ready to paste)"
            )
        if missing:
            raise ValueError(
                "connected AWS is enabled but its configuration is incomplete: missing "
                + ", ".join(missing)
            )
        return self


class AwsCapacityEnvironmentSettings(BaseSettings):
    """The managed-capacity value no release can publish.

    What an instance hour costs is the deployment's own decision. The worker
    image, agent artifact URL and both baked AMI catalogs are facts of the release
    it points at — GPU AMIs became one when the release started baking them, and
    a deployment naming its own would be asserting a driver the release never
    built.
    """

    instance_hourly_micros: dict[str, int] = Field(default_factory=dict)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_AWS_CAPACITY_",
        extra="ignore",
    )


class AwsCapacitySettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    worker_image_digest: str = ""
    agent_binary_url: str = ""
    cpu_ami_ids: dict[str, str] = Field(default_factory=dict)
    gpu_ami_ids: dict[str, str] = Field(default_factory=dict)
    instance_hourly_micros: dict[str, int] = Field(default_factory=dict)

    @field_validator("worker_image_digest")
    @classmethod
    def normalize_worker_image_digest(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("agent_binary_url")
    @classmethod
    def validate_agent_binary_url(cls, value: str) -> str:
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
    def validate_ami_catalogs(cls, value: dict[str, str]) -> dict[str, str]:
        return normalize_ami_catalog(value)

    @field_validator("instance_hourly_micros")
    @classmethod
    def validate_instance_prices(cls, value: dict[str, int]) -> dict[str, int]:
        return normalize_instance_prices(value)

    @property
    def configured(self) -> bool:
        """Whether managed AWS capacity can launch anything at all.

        GPU AMIs are deliberately absent from this. Requiring them made a
        CPU-only deployment report unconfigured, which emptied the compute
        catalog and left the API advertising no AWS regions — a deployment that
        wanted no GPUs could not use AWS at all. A region without a GPU AMI
        simply offers no GPU instance types there.
        """
        return bool(
            self.worker_image_digest
            and self.agent_binary_url
            and self.cpu_ami_ids
            and self.instance_hourly_micros
        )

    @model_validator(mode="after")
    def validate_atomic_configuration(self) -> AwsCapacitySettings:
        """Managed AWS capacity is all five values or none of them.

        The release always carries the artifacts, so their presence would make
        this rule true for every deployment pointing at one and would hide a pool
        that can never launch. Intent is therefore read from the one value only a
        deployment can author — what an instance hour costs. Authoring it demands
        the rest; leaving it out keeps managed capacity off whatever the release
        published.
        """

        if not self.instance_hourly_micros:
            return self
        missing_from_release = [
            name
            for name, value in (
                ("worker image digest", self.worker_image_digest),
                ("agent artifact URL", self.agent_binary_url),
                ("regional CPU AMI catalog", self.cpu_ami_ids),
            )
            if not value
        ]
        missing_from_deployment = [
            name
            for name, value in (
                (f"instance price estimates ({_INSTANCE_PRICES_ENV})", self.instance_hourly_micros),
            )
            if not value
        ]
        problems: list[str] = []
        if missing_from_release:
            problems.append(
                f"the release at {_MANIFEST_URL_ENV} published no "
                + ", ".join(missing_from_release)
            )
        if missing_from_deployment:
            problems.append("this deployment authored no " + ", ".join(missing_from_deployment))
        if problems:
            raise ValueError("AWS capacity configuration is incomplete: " + "; ".join(problems))
        return self

    def binaries_by_region(
        self,
        agent_artifact: AgentBinarySettings,
    ) -> dict[str, AwsManagedPoolBinaries]:
        agent_version, agent_sha256 = agent_artifact.require_amd64()
        if not self.worker_image_digest:
            raise ValueError("AWS capacity is not configured")
        regions = sorted(self.cpu_ami_ids.keys() | self.gpu_ami_ids.keys())
        if not regions or not self.instance_hourly_micros:
            raise ValueError("AWS capacity is not configured")
        return {
            region: AwsManagedPoolBinaries(
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
        extra="ignore",
    )


__all__ = [
    "AwsAccountConnectionEnvironmentSettings",
    "AwsAccountConnectionSettings",
    "AwsCapacityEnvironmentSettings",
    "AwsCapacityReconciliationSettings",
    "AwsCapacitySettings",
]
