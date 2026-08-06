from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest
from agent.binary import AgentBinarySettings
from provider_clients.settings import (
    AwsAccountConnectionSettings,
    AwsCapacitySettings,
)
from pydantic import ValidationError
from shared.aws_connections import AwsAccountConnection


def _no_connections(_workspace_id: str) -> Iterable[AwsAccountConnection]:
    return ()


def test_aws_connection_settings_reject_invalid_control_authority() -> None:
    with pytest.raises(ValidationError, match="control principal ARN is invalid"):
        AwsAccountConnectionSettings(
            template_url="https://assets.example.com/template.json",
            control_principal_arn="not-an-arn",
        )


def test_aws_connection_settings_reject_enabled_without_control_authority() -> None:
    # Enabling with a template URL but no control principal would publish a customer
    # authorization template that trusts nothing.
    with pytest.raises(ValidationError, match="configuration is incomplete"):
        AwsAccountConnectionSettings(
            enabled=True,
            template_url="https://assets.example.com/template.json",
        )


def test_aws_capacity_settings_reject_partial_and_mutable_artifacts() -> None:
    # Authoring the deployment's half without a release to complete it is the
    # half-configured pool this rule exists to stop. Prices are that half: GPU
    # AMIs stopped being an intent signal when the release began publishing them.
    with pytest.raises(ValidationError, match="published no worker image digest"):
        AwsCapacitySettings(instance_hourly_micros={"g6.xlarge": 800_000})

    # A release publishes three of the five, so it must not be able to turn
    # managed capacity on by itself.
    released_only = AwsCapacitySettings(
        worker_image_digest=f"registry.example.com/worker@sha256:{'c' * 64}",
        agent_binary_url=(
            f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'b' * 64}/"
            "lazycloud-agent-linux-amd64"
        ),
        cpu_ami_ids={"us-east-1": "ami-0123456789abcdef0"},
    )
    assert not released_only.configured

    capacity = AwsCapacitySettings(
        worker_image_digest="registry.example.com/worker:latest",
        agent_binary_url=(
            f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'b' * 64}/"
            "lazycloud-agent-linux-amd64"
        ),
        cpu_ami_ids={"us-east-1": "ami-0123456789abcdef0"},
        gpu_ami_ids={"us-east-1": "ami-0fedcba9876543210"},
        instance_hourly_micros={"i4i.xlarge": 340_000},
    )
    artifact = AgentBinarySettings(
        binary_dir=Path("/opt/lazycloud/agent"),
        binary_version="0.1.0",
        binary_sha256_by_arch={"amd64": "b" * 64},
    )

    with pytest.raises(ValidationError, match="worker_image_digest"):
        capacity.binaries_by_region(artifact)


def test_a_deployment_that_wants_no_gpus_can_still_use_aws() -> None:
    """Requiring a GPU catalog turned "no GPUs" into "no AWS".

    `configured` gates the whole compute catalog, so a CPU-only deployment
    reported unconfigured, advertised no regions, and had every AWS instance type
    rejected by compute policy — for want of a GPU it never asked for.
    """
    capacity = AwsCapacitySettings(
        worker_image_digest=f"registry.example.com/worker@sha256:{'c' * 64}",
        agent_binary_url=(
            f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'b' * 64}/"
            "lazycloud-agent-linux-amd64"
        ),
        cpu_ami_ids={"us-east-1": "ami-0123456789abcdef0"},
        instance_hourly_micros={"i4i.xlarge": 340_000},
    )

    assert capacity.configured
    assert not capacity.gpu_ami_ids
