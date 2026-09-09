from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from urllib.parse import urlparse

from agent.binary import AgentBinarySettings
from provider_aws import AwsManagedPoolBinaries, AwsRegionalPrices
from provider_hetzner import HetznerNodeImage
from provider_hetzner.capacity_policy import HETZNER_CAPACITY_POLICY
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

from .release_manifest import WORKER_IMAGE_PATTERN

_AMI_PATTERN = re.compile(r"ami-[0-9a-f]{8,17}")
_AWS_PRINCIPAL_PATTERN = re.compile(
    r"arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:"
    r"(root|role/[A-Za-z0-9+=,.@_/-]+|user/[A-Za-z0-9+=,.@_/-]+)"
)
_AWS_REGION_PATTERN = re.compile(r"(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+")
RELEASE_MANIFEST_URL_ENV = f"{ENV_PREFIX}_RELEASE_MANIFEST_URL"
AWS_CONNECTION_CONTROL_PRINCIPAL_ENV = f"{ENV_PREFIX}_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN"


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
        if hourly_micros <= 0:
            # Zero is the dangerous one. Offers are ranked by cost per node, so a
            # free instance hour wins every comparison it is entered in, and the
            # AWS price list answers zero for types with no published on-demand
            # rate rather than declining to answer. An instance whose price is
            # not known is left out of this map and is simply not offered.
            raise ValueError(f"AWS capacity instance price for {instance_type!r} must be positive")
        normalized[instance_type] = hourly_micros
    return normalized


class AwsAccountConnectionEnvironmentSettings(BaseSettings):
    """The connected-AWS choices a deployment authors.

    The customer authorization template is published by a release, not chosen
    here, so its URL is resolved from the release manifest.
    """

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
        """Whether this deployment has a connected AWS at all.

        Read from the two values that do the work rather than from a flag beside
        them. A separate switch can disagree with them, and did: it defaulted
        false, no deployment path ever set it, and every deployment that had both
        values still answered 503 to every connection request.
        """
        return bool(self.template_url and self.control_principal_arn)

    @model_validator(mode="after")
    def validate_configuration(self) -> AwsAccountConnectionSettings:
        """Only the shape of a value is checked here, never whether it is present.

        Holding one half is an ordinary, reachable state rather than a mistake: a
        deployment's infrastructure always publishes the control principal, while
        the template arrives from a release the deployment may not name yet, and
        the deploy warns and carries on when it does not. Raising on that
        combination stopped the control plane from starting at all, on exactly the
        first deploy of every new deployment.

        Absence is answered where it can say something useful. `configured` is
        false without both, and the route that needs connections refuses with the
        names of what is missing, which is a 503 on one capability rather than a
        process that will not boot.
        """

        if self.control_principal_arn and (
            _AWS_PRINCIPAL_PATTERN.fullmatch(self.control_principal_arn) is None
        ):
            raise ValueError("AWS account connection control principal ARN is invalid")
        return self


class AwsCapacityEnvironmentSettings(BaseSettings):
    """Deployment-owned supplier rates, separate from released host artifacts."""

    instance_hourly_micros: dict[str, int] = Field(default_factory=dict)
    regional_prices: dict[str, AwsRegionalPrices] = Field(default_factory=dict)

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
    regional_prices: dict[str, AwsRegionalPrices] = Field(default_factory=dict)

    @field_validator("worker_image_digest")
    @classmethod
    def validate_worker_image_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and WORKER_IMAGE_PATTERN.fullmatch(normalized) is None:
            raise ValueError("worker_image_digest must name an immutable sha256 digest")
        return normalized

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
        """Configured instance prices require host artifacts and complete regional prices."""

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
        if missing_from_release:
            raise ValueError(
                "AWS capacity configuration is incomplete: "
                f"the release at {RELEASE_MANIFEST_URL_ENV} published no "
                + ", ".join(missing_from_release)
            )
        missing_prices = (
            self.cpu_ami_ids.keys() | self.gpu_ami_ids.keys()
        ) - self.regional_prices.keys()
        if missing_prices:
            raise ValueError(
                f"{ENV_PREFIX}_AWS_CAPACITY_REGIONAL_PRICES has no storage and IPv4 prices for "
                + ", ".join(sorted(missing_prices))
            )
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


class HetznerCapacityBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    ref: str = Field(pattern=r"^hetzner:[a-z0-9][a-z0-9-]{0,119}$")
    workspace: str = Field(default="default", min_length=1)
    images_by_location: dict[str, HetznerNodeImage]
    usd_per_currency_unit: Decimal = Field(gt=0)
    primary_ipv4_hourly_micros: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_binding(self) -> HetznerCapacityBinding:
        if not set(HETZNER_CAPACITY_POLICY.allowed_regions) <= self.images_by_location.keys():
            raise ValueError(f"{self.ref} requires a release image for every allowed location")
        return self


class PlatformCapacitySettings(BaseSettings):
    hetzner: tuple[HetznerCapacityBinding, ...] = ()
    hetzner_tokens: dict[str, SecretStr] = Field(default_factory=dict, repr=False)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_PLATFORM_CAPACITY_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    @model_validator(mode="after")
    def validate_bindings(self) -> PlatformCapacitySettings:
        if len({binding.ref for binding in self.hetzner}) != len(self.hetzner):
            raise ValueError("platform provider refs must be unique")
        if len({binding.workspace for binding in self.hetzner}) > 1:
            raise ValueError("platform providers require one capacity workspace")
        for binding in self.hetzner:
            token = self.hetzner_tokens.get(binding.ref)
            if token is None or not token.get_secret_value().strip():
                raise ValueError(f"{binding.ref} requires a provider token")
        if HETZNER_CAPACITY_POLICY.warm_cpu_min > 0 and len(self.hetzner) > 1:
            raise ValueError("only one platform provider may own the automatic warm floor")
        return self

    @property
    def configured(self) -> bool:
        return bool(self.hetzner)


__all__ = [
    "AWS_CONNECTION_CONTROL_PRINCIPAL_ENV",
    "RELEASE_MANIFEST_URL_ENV",
    "AwsAccountConnectionEnvironmentSettings",
    "AwsAccountConnectionSettings",
    "AwsCapacityEnvironmentSettings",
    "AwsCapacityReconciliationSettings",
    "AwsCapacitySettings",
    "HetznerCapacityBinding",
    "PlatformCapacitySettings",
]
