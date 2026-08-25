"""Resolution of a deployment's configuration from the release it points at.

A release publishes the agent artifact, its digest, the URL that serves it, the
container-worker image, the customer authorization template, and any baked CPU
AMIs. Those are facts of one release, and a deployment that copies them into six
independent variables can hold five from one release and one from another with
nothing able to notice. Pointing at the manifest instead makes them arrive
together or not at all.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from agent.binary import AgentBinaryEnvironmentSettings, AgentBinarySettings
from provider_aws import aws_account_connection_template_identity
from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.transport_retry import TransientRetryPolicy, call_with_transient_retry

from provider_clients.release_manifest import AwsReleaseManifest
from provider_clients.settings import (
    AwsAccountConnectionEnvironmentSettings,
    AwsAccountConnectionSettings,
    AwsCapacityEnvironmentSettings,
    AwsCapacitySettings,
)

_MAXIMUM_MANIFEST_BYTES = 1024 * 1024
_TRANSFER_CHUNK_BYTES = 1024 * 1024


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


def materialize_agent_artifact(
    manifest: AwsReleaseManifest,
    *,
    into: Path,
    timeout_seconds: float = 300.0,
) -> Path:
    """Put the release's agent binary where the install routes serve it from.

    The manifest names the artifact and the deployment says where it keeps one,
    so neither knows the whole path alone. The layout is the release's, not a
    convention repeated here: the object's own `local_path` carries the file
    name, and the version directory is what `/install` appends before opening
    it.

    Idempotent, because this runs before every replica and a pod that restarts
    should not re-fetch what it already holds. An artifact already present and
    already matching its digest is left alone; one present and not matching is
    replaced, since a release is immutable and the wrong bytes under the right
    name can only be a bad transfer.
    """

    artifact = manifest.agent_artifact_object
    destination = into / manifest.agent_artifact_version / PurePosixPath(artifact.local_path).name
    if destination.is_file() and _file_sha256(destination) == artifact.sha256:
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(f".{destination.name}.partial")
    digest = hashlib.sha256()
    request = urllib.request.Request(
        artifact.public_url,
        method="GET",
        headers={"User-Agent": "lazycloud-release-resolver/1"},
    )

    def transfer() -> None:
        # Re-opened per attempt, and the digest and the staged file start over
        # with it: a retry that resumed into a half-written file would hash bytes
        # from two transfers and fail the check that exists to catch exactly that.
        nonlocal digest
        digest = hashlib.sha256()
        with (
            urllib.request.urlopen(request, timeout=timeout_seconds) as response,
            staged.open("wb") as handle,
        ):
            while chunk := response.read(_TRANSFER_CHUNK_BYTES):
                digest.update(chunk)
                handle.write(chunk)

    try:
        call_with_transient_retry(
            transfer,
            policy=ARTIFACT_FETCH_RETRY_POLICY,
            sleep=_retry_sleep,
        )
    except OSError as exc:
        staged.unlink(missing_ok=True)
        raise ReleaseManifestError(
            f"agent artifact is not readable: {artifact.public_url}"
        ) from exc

    written = staged.stat().st_size
    if written != artifact.size_bytes:
        staged.unlink(missing_ok=True)
        raise ReleaseManifestError(
            f"agent artifact is {written} bytes, and release "
            f"{manifest.release_version} publishes {artifact.size_bytes}"
        )
    if digest.hexdigest() != artifact.sha256:
        staged.unlink(missing_ok=True)
        raise ReleaseManifestError(
            f"agent artifact does not match the digest release "
            f"{manifest.release_version} publishes for it"
        )

    # Executable, because this is the file a node downloads and runs. Replaced by
    # rename so a reader never opens a partial one.
    staged.chmod(0o755)
    os.replace(staged, destination)
    return destination


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_TRANSFER_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


LOGGER = logging.getLogger(__name__)

RELEASE_FETCH_RETRY_POLICY = TransientRetryPolicy(
    max_attempts=6,
    base_delay_seconds=0.5,
    max_delay_seconds=5.0,
    budget_seconds=60.0,
)
"""How hard to try before calling the release unreachable.

Startup gets the same tolerance as steady state. A process that resolves this
once and exits on the first refused connection turns a moment of packet loss
into a crash loop, and a crash loop into an outage that outlasts its cause by
however long nobody is watching. Bounded, because a release that is genuinely
gone has to be said out loud rather than waited on forever.
"""

ARTIFACT_FETCH_RETRY_POLICY = TransientRetryPolicy(
    max_attempts=6,
    base_delay_seconds=0.5,
    max_delay_seconds=5.0,
    budget_seconds=180.0,
)
"""Longer budget, because each attempt moves tens of megabytes.

A transfer that dies near its end has spent minutes to get there and is worth
one more try; the budget still bounds the whole thing well inside the transfer
timeout above it.
"""


def _retry_sleep(delay: float) -> None:
    LOGGER.warning("release transfer failed; retrying in %.1fs", delay)
    time.sleep(delay)


def _download_manifest(url: str, *, timeout_seconds: float) -> bytes:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "lazycloud-release-resolver/1"},
    )

    def read_manifest() -> bytes:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.read(_MAXIMUM_MANIFEST_BYTES + 1)

    try:
        payload = call_with_transient_retry(
            read_manifest,
            policy=RELEASE_FETCH_RETRY_POLICY,
            sleep=_retry_sleep,
        )
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
    "materialize_agent_artifact",
    "resolve_deployment_release",
]
