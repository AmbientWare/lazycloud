from __future__ import annotations

import json
from pathlib import Path

from agent.binary import AgentBinaryEnvironmentSettings
from provider_aws import aws_account_connection_template_identity
from provider_clients.release import deployment_release
from provider_clients.release_manifest import (
    AGENT_AMD64_FILENAME,
    SCHEMA_VERSION,
    AwsReleaseManifest,
    ReleaseObject,
    s3_public_url,
)
from provider_clients.settings import (
    AwsAccountConnectionEnvironmentSettings,
    AwsCapacityEnvironmentSettings,
)

_BUCKET = "lazycloud-releases"
_REGION = "us-east-1"
_PREFIX = "connected-aws"
_VERSION = "2026.07.14"
_AGENT_SHA256 = "a" * 64
_WORKER_IMAGE = f"registry.example.com/container-worker@sha256:{'b' * 64}"
_CPU_AMI_IDS = {"us-east-1": "ami-0123456789abcdef0"}
_GPU_AMI_IDS = {"us-east-1": "ami-0fedcba9876543210"}


def _release_manifest() -> AwsReleaseManifest:
    identity = aws_account_connection_template_identity()
    template_key = f"{_PREFIX}/connection-templates/{identity.sha256}/template.json"
    agent_key = f"{_PREFIX}/agents/{_VERSION}/{_AGENT_SHA256}/{AGENT_AMD64_FILENAME}"
    manifest_key = f"{_PREFIX}/releases/{_VERSION}/manifest.json"
    template_url = s3_public_url(_BUCKET, _REGION, template_key)
    agent_url = s3_public_url(_BUCKET, _REGION, agent_key)
    return AwsReleaseManifest(
        schema_version=SCHEMA_VERSION,
        release_version=_VERSION,
        bucket=_BUCKET,
        region=_REGION,
        manifest_object_key=manifest_key,
        manifest_public_url=s3_public_url(_BUCKET, _REGION, manifest_key),
        connection_template_version=identity.version,
        connection_template_sha256=identity.sha256,
        agent_artifact_version=_VERSION,
        agent_artifact_sha256=_AGENT_SHA256,
        container_worker_image=_WORKER_IMAGE,
        capacity_cpu_ami_ids=_CPU_AMI_IDS,
        capacity_gpu_ami_ids=_GPU_AMI_IDS,
        objects=[
            ReleaseObject(
                local_path="objects/connection-template.json",
                object_key=template_key,
                public_url=template_url,
                sha256=identity.sha256,
                size_bytes=1024,
                content_type="application/json",
            ),
            ReleaseObject(
                local_path=f"agent-binarys/{_VERSION}/{AGENT_AMD64_FILENAME}",
                object_key=agent_key,
                public_url=agent_url,
                sha256=_AGENT_SHA256,
                size_bytes=47_000_000,
                content_type="application/octet-stream",
            ),
        ],
        deployment_environment={
            "LAZYCLOUD_AGENT_BINARY_SHA256_BY_ARCH": json.dumps(
                {"amd64": _AGENT_SHA256}, sort_keys=True, separators=(",", ":")
            ),
            "LAZYCLOUD_AGENT_BINARY_VERSION": _VERSION,
            "LAZYCLOUD_AWS_CAPACITY_AGENT_BINARY_URL": agent_url,
            "LAZYCLOUD_AWS_CAPACITY_WORKER_IMAGE_DIGEST": _WORKER_IMAGE,
            "LAZYCLOUD_AWS_CAPACITY_CPU_AMI_IDS": json.dumps(
                _CPU_AMI_IDS, sort_keys=True, separators=(",", ":")
            ),
            "LAZYCLOUD_AWS_CAPACITY_GPU_AMI_IDS": json.dumps(
                _GPU_AMI_IDS, sort_keys=True, separators=(",", ":")
            ),
            "LAZYCLOUD_AWS_CONNECTION_TEMPLATE_URL": template_url,
        },
    )


def test_release_supplies_every_artifact_value_a_deployment_would_copy() -> None:
    """One pointer resolves to the six values, so they cannot come from two releases."""

    release = deployment_release(
        _release_manifest(),
        agent_binaries=AgentBinaryEnvironmentSettings(binary_dir=Path("/var/lib/lazycloud/agent")),
        aws_capacity=AwsCapacityEnvironmentSettings(
            instance_hourly_micros={"i4i.xlarge": 340_000},
        ),
        aws_connections=AwsAccountConnectionEnvironmentSettings(
            enabled=True,
            control_principal_arn="arn:aws:iam::123456789012:role/control-plane",
        ),
    )

    assert release.version == _VERSION
    assert release.agent_binaries.binary_version == _VERSION
    assert release.agent_binaries.binary_sha256_by_arch == {"amd64": _AGENT_SHA256}
    assert release.aws_capacity.worker_image_digest == _WORKER_IMAGE
    assert release.aws_capacity.cpu_ami_ids == _CPU_AMI_IDS
    # The GPU catalog now arrives from the release too, so a deployment cannot
    # name a GPU AMI carrying a driver the release never baked.
    assert release.aws_capacity.gpu_ami_ids == _GPU_AMI_IDS
    assert release.aws_capacity.agent_binary_url.endswith(
        f"/agents/{_VERSION}/{_AGENT_SHA256}/{AGENT_AMD64_FILENAME}"
    )
    assert aws_account_connection_template_identity().sha256 in (
        release.aws_connections.template_url
    )
    assert release.aws_capacity.configured
    assert release.aws_connections.configured


def test_deployment_without_a_release_keeps_the_unmanaged_shape() -> None:
    release = deployment_release(
        None,
        agent_binaries=AgentBinaryEnvironmentSettings(),
        aws_capacity=AwsCapacityEnvironmentSettings(),
        aws_connections=AwsAccountConnectionEnvironmentSettings(),
    )

    assert release.version == ""
    assert release.agent_binaries.binary_version == ""
    assert not release.aws_capacity.configured
    assert not release.aws_connections.configured
