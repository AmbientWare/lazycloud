"""Submit the account's reviewed connection stack with customer AWS credentials."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field
from shared.aws_connections import AwsConnectionStackAction


class _CallerIdentity(BaseModel):
    account_id: str = Field(alias="Account")


class _CreatedStack(BaseModel):
    stack_id: str = Field(alias="StackId")


def create_connection_stack(action: AwsConnectionStackAction, *, profile: str | None) -> str:
    arguments = ["aws", "--region", action.region]
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
    with tempfile.TemporaryDirectory(prefix="lazycloud-aws-") as directory:
        request = Path(directory) / "connection.json"
        request.write_text(action.request.model_dump_json(), encoding="utf-8")
        request.chmod(0o600)
        created = subprocess.run(
            [
                *arguments,
                "cloudformation",
                "create-stack",
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
