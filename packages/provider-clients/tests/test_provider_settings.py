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


def test_aws_connection_settings_reject_half_configured_authority() -> None:
    # A template URL without a control principal would publish a customer
    # authorization template that trusts nothing.
    with pytest.raises(ValidationError, match="configuration is incomplete"):
        AwsAccountConnectionSettings(template_url="https://assets.example.com/template.json")


def test_aws_capacity_settings_reject_partial_and_mutable_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The settings read a cwd-relative `.env`; an acceptance `.env` at the repo
    # root would complete the deliberately partial configuration under test.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="configuration is incomplete"):
        AwsCapacitySettings(worker_image_digest=f"registry.example.com/worker@sha256:{'c' * 64}")

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
        artifact_version="0.1.0",
        sha256_by_arch={"amd64": "b" * 64},
    )

    with pytest.raises(ValidationError, match="worker_image_digest"):
        capacity.binaries_by_region(artifact)
