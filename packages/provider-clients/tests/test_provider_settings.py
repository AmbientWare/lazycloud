from __future__ import annotations

import pytest
from provider_aws import AwsRegionalPrices
from provider_clients.settings import (
    AwsAccountConnectionSettings,
    AwsCapacitySettings,
)
from pydantic import ValidationError


def test_aws_connection_settings_reject_invalid_control_authority() -> None:
    with pytest.raises(ValidationError, match="control principal ARN is invalid"):
        AwsAccountConnectionSettings(
            template_url="https://assets.example.com/template.json",
            control_principal_arn="not-an-arn",
        )


def test_a_control_principal_without_a_release_is_not_an_error() -> None:
    """The first deploy of every deployment, and it must not stop the process.

    Infrastructure always publishes the control principal; the template arrives
    from a release the deployment may not name yet, and the deploy warns and
    carries on when it does not. Raising on that pair took the control plane down
    at boot rather than leaving one capability unavailable, which is the failure
    a settings validator is least able to explain and most able to cause.
    """
    waiting = AwsAccountConnectionSettings(
        control_principal_arn="arn:aws:iam::123456789012:role/control-plane",
    )

    assert not waiting.configured
    assert not AwsAccountConnectionSettings().configured
    assert AwsAccountConnectionSettings(
        control_principal_arn="arn:aws:iam::123456789012:role/control-plane",
        template_url="https://assets.example.com/template.json",
    ).configured


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
            f"https://releases.example.com/agents/0.1.0/{'b' * 64}/lazycloud-agent-linux-amd64"
        ),
        cpu_ami_ids={"us-east-1": "ami-0123456789abcdef0"},
    )
    assert not released_only.configured

    with pytest.raises(ValidationError, match="worker_image_digest"):
        AwsCapacitySettings(worker_image_digest="registry.example.com/worker:latest")


def test_a_deployment_that_wants_no_gpus_can_still_use_aws() -> None:
    """Requiring a GPU catalog turned "no GPUs" into "no AWS".

    `configured` gates the whole compute catalog, so a CPU-only deployment
    reported unconfigured, advertised no regions, and had every AWS instance type
    rejected by compute policy — for want of a GPU it never asked for.
    """
    capacity = AwsCapacitySettings(
        worker_image_digest=f"registry.example.com/worker@sha256:{'c' * 64}",
        agent_binary_url=(
            f"https://releases.example.com/agents/0.1.0/{'b' * 64}/lazycloud-agent-linux-amd64"
        ),
        cpu_ami_ids={"us-east-1": "ami-0123456789abcdef0"},
        instance_hourly_micros={"m7i.xlarge": 340_000},
        regional_prices={
            "us-east-1": AwsRegionalPrices(
                gp3_gib_monthly_micros=80_000, public_ipv4_hourly_micros=5_000
            )
        },
    )

    assert capacity.configured
    assert not capacity.gpu_ami_ids
