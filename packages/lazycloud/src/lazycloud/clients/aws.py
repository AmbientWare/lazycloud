"""Submit the account's reviewed connection stack with customer AWS credentials."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field
from shared.aws_connections import AwsConnectionStackAction, AwsStackParameter


class _CallerIdentity(BaseModel):
    account_id: str = Field(alias="Account")


class _CreatedStack(BaseModel):
    stack_id: str = Field(alias="StackId")


class _AvailabilityZone(BaseModel):
    name: str = Field(alias="ZoneName", min_length=1)


class _AvailabilityZones(BaseModel):
    zones: list[_AvailabilityZone] = Field(alias="AvailabilityZones")


def create_connection_stack(
    action: AwsConnectionStackAction,
    *,
    profile: str | None,
    execution_role_arn: str | None = None,
    aws_cli: str = "aws",
) -> str:
    arguments = [aws_cli, "--region", action.region]
    if profile is not None:
        arguments.extend(["--profile", profile])
    environment = {**os.environ, "AWS_PAGER": ""}
    identity = subprocess.run(
        [*arguments, "sts", "get-caller-identity", "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=60,
    )
    if identity.returncode != 0:
        raise RuntimeError(
            "AWS credentials could not be verified; sign in with the customer profile"
        )
    if _CallerIdentity.model_validate_json(identity.stdout).account_id != action.account_id:
        raise RuntimeError("AWS credentials belong to a different account; no stack was created")
    discovered = subprocess.run(
        [
            *arguments,
            "ec2",
            "describe-availability-zones",
            "--filters",
            "Name=zone-type,Values=availability-zone",
            "Name=state,Values=available",
            "Name=opt-in-status,Values=opt-in-not-required,opted-in",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=60,
    )
    if discovered.returncode != 0:
        raise RuntimeError(
            "AWS availability zones could not be discovered; the customer profile needs "
            "ec2:DescribeAvailabilityZones permission. No stack was created."
        )
    zones = sorted(
        {zone.name for zone in _AvailabilityZones.model_validate_json(discovered.stdout).zones}
    )
    if len(zones) < 2:
        raise RuntimeError(
            f"AWS region {action.region} needs two available standard zones; "
            f"this account has {len(zones)}. No stack was created."
        )
    parameters = (
        *action.request.Parameters,
        AwsStackParameter(ParameterKey="AvailabilityZoneA", ParameterValue=zones[0]),
        AwsStackParameter(ParameterKey="AvailabilityZoneB", ParameterValue=zones[1]),
    )
    stack_request = action.request.model_copy(update={"Parameters": parameters})
    create_arguments = [*arguments, "cloudformation", "create-stack"]
    if execution_role_arn is not None:
        create_arguments.extend(["--role-arn", execution_role_arn])
    with tempfile.TemporaryDirectory(prefix="lazycloud-aws-") as directory:
        request = Path(directory) / "connection.json"
        request.write_text(stack_request.model_dump_json(), encoding="utf-8")
        request.chmod(0o600)
        created = subprocess.run(
            [
                *create_arguments,
                "--cli-input-json",
                f"file://{request}",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=120,
        )
    if created.returncode != 0:
        raise RuntimeError(
            "CloudFormation did not accept the connection stack. Check the customer's AWS "
            "permissions and existing stack before retrying."
        )
    return _CreatedStack.model_validate_json(created.stdout).stack_id
