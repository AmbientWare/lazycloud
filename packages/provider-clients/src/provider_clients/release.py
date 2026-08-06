"""Resolution of a deployment's configuration from the release it points at.

A release publishes the agent artifact, its digest, the URL that serves it, the
container-worker image, the customer authorization template, and any baked CPU
AMIs. Those are facts of one release, and a deployment that copies them into six
independent variables can hold five from one release and one from another with
nothing able to notice. Pointing at the manifest instead makes them arrive
together or not at all.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from agent.binary import AgentBinaryEnvironmentSettings, AgentBinarySettings
from provider_aws import aws_account_connection_template_identity
from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

from provider_clients.release_manifest import AwsReleaseManifest
from provider_clients.settings import (
    AwsAccountConnectionEnvironmentSettings,
    AwsAccountConnectionSettings,
    AwsCapacityEnvironmentSettings,
    AwsCapacitySettings,
)

_MAXIMUM_MANIFEST_BYTES = 1024 * 1024


class ReleaseManifestError(RuntimeError):
    """A configured release manifest could not be read or does not fit this build."""


class ReleaseManifestSettings(BaseSettings):
    manifest_url: str = ""
    fetch_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_RELEASE_",
        extra="ignore",
    )

    @field_validator("manifest_url")
    @classmethod
    def validate_manifest_url(cls, value: str) -> str:
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
            raise ValueError("release manifest URL must be an HTTPS URL without credentials")
        return url


@dataclass(frozen=True, slots=True)
class DeploymentRelease:
    version: str
    agent_binaries: AgentBinarySettings
    aws_capacity: AwsCapacitySettings
    aws_connections: AwsAccountConnectionSettings

    def describe(self) -> str:
        return (
            f"release {self.version or 'none'}: agent artifact "
            f"{self.agent_binaries.binary_version or 'none'}, AWS managed capacity "
            f"{'configured' if self.aws_capacity.configured else 'not configured'}, "
            f"connected AWS {'configured' if self.aws_connections.configured else 'not configured'}"
        )


def resolve_deployment_release(
    settings: ReleaseManifestSettings | None = None,
) -> DeploymentRelease:
    manifest_settings = settings or ReleaseManifestSettings()
    # A deployment with no manifest has no release facts to hold, which is the
    # shape of every deployment that runs no managed capacity and serves no
    # published agent artifact. It reaches the same construction below with an
    # absent release rather than a second way of being configured.
    manifest = (
        fetch_release_manifest(
            manifest_settings.manifest_url,
            timeout_seconds=manifest_settings.fetch_timeout_seconds,
        )
        if manifest_settings.manifest_url
        else None
    )
    return deployment_release(
        manifest,
        agent_binaries=AgentBinaryEnvironmentSettings(),
        aws_capacity=AwsCapacityEnvironmentSettings(),
        aws_connections=AwsAccountConnectionEnvironmentSettings(),
    )


def deployment_release(
    manifest: AwsReleaseManifest | None,
    *,
    agent_binaries: AgentBinaryEnvironmentSettings,
    aws_capacity: AwsCapacityEnvironmentSettings,
    aws_connections: AwsAccountConnectionEnvironmentSettings,
) -> DeploymentRelease:
    return DeploymentRelease(
        version="" if manifest is None else manifest.release_version,
        agent_binaries=AgentBinarySettings(
            binary_dir=agent_binaries.binary_dir,
            binary_name=agent_binaries.binary_name,
            binary_version="" if manifest is None else manifest.agent_artifact_version,
            binary_sha256_by_arch=(
                {} if manifest is None else manifest.agent_artifact_sha256_by_arch
            ),
        ),
        aws_capacity=AwsCapacitySettings(
            worker_image_digest="" if manifest is None else manifest.container_worker_image,
            agent_binary_url="" if manifest is None else manifest.agent_artifact_object.public_url,
            cpu_ami_ids={} if manifest is None else manifest.capacity_cpu_ami_ids,
            gpu_ami_ids={} if manifest is None else manifest.capacity_gpu_ami_ids,
            instance_hourly_micros=aws_capacity.instance_hourly_micros,
        ),
        aws_connections=AwsAccountConnectionSettings(
            enabled=aws_connections.enabled,
            template_url=(
                "" if manifest is None else manifest.connection_template_object.public_url
            ),
            control_principal_arn=aws_connections.control_principal_arn,
            external_id_bytes=aws_connections.external_id_bytes,
            draft_ttl_seconds=aws_connections.draft_ttl_seconds,
            cleanup_tombstone_ttl_seconds=aws_connections.cleanup_tombstone_ttl_seconds,
            cleanup_timeout_seconds=aws_connections.cleanup_timeout_seconds,
        ),
    )


def fetch_release_manifest(url: str, *, timeout_seconds: float = 15.0) -> AwsReleaseManifest:
    payload = _download_manifest(url, timeout_seconds=timeout_seconds)
    try:
        manifest = AwsReleaseManifest.model_validate_json(payload)
    except ValidationError as exc:
        raise ReleaseManifestError(f"release manifest at {url} is not a valid release") from exc
    if manifest.manifest_public_url != url:
        raise ReleaseManifestError(
            f"release manifest at {url} publishes itself as {manifest.manifest_public_url}"
        )
    # The customer authorization template is validated against the copy this
    # build bundles, so a release whose template differs is a release this
    # binary cannot serve, however well formed the manifest is.
    identity = aws_account_connection_template_identity()
    if (
        manifest.connection_template_version != identity.version
        or manifest.connection_template_sha256 != identity.sha256
    ):
        raise ReleaseManifestError(
            f"release {manifest.release_version} carries connection template "
            f"{manifest.connection_template_version} but this build bundles {identity.version}"
        )
    return manifest


def _download_manifest(url: str, *, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "lazycloud-release-resolver/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(_MAXIMUM_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise ReleaseManifestError(f"release manifest is not readable: {url}") from exc
    if len(payload) > _MAXIMUM_MANIFEST_BYTES:
        raise ReleaseManifestError(
            f"release manifest exceeds {_MAXIMUM_MANIFEST_BYTES} bytes: {url}"
        )
    return payload


__all__ = [
    "DeploymentRelease",
    "ReleaseManifestError",
    "ReleaseManifestSettings",
    "deployment_release",
    "fetch_release_manifest",
    "resolve_deployment_release",
]
