"""Operator automation for the customer connected-AWS CloudFormation actions.

The public API returns a typed stack request for connecting an account and
console actions for cleanup. This deployment command validates each action
against the exact verified release template and platform principal, then
applies it with the ambient operator credentials. It creates or deletes only
``compute-connection-*-g*`` stacks and optionally passes the stack-owned
CloudFormation execution role.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from provider_aws import (
    aws_account_connection_template_identity,
    parse_aws_connection_stack_cleanup_action,
)
from pydantic import BaseModel, Field, RootModel
from shared.aws_connections import AwsConnectionStackAction

_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_CREATE_WAIT_STATES = frozenset({"CREATE_IN_PROGRESS", "REVIEW_IN_PROGRESS"})
_DELETE_WAIT_STATES = frozenset({"DELETE_IN_PROGRESS", "CREATE_COMPLETE"})


class CustomerStackError(RuntimeError):
    pass


class AwsCallerIdentity(BaseModel):
    account_id: str = Field(alias="Account")


class StackStatusDocument(RootModel[tuple[str, str | None]]):
    pass


def _run_aws(
    aws_cli: str,
    arguments: Sequence[str],
    *,
    allow_missing_stack: bool = False,
) -> object:
    result = subprocess.run(
        [aws_cli, *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "AWS_PAGER": ""},
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if allow_missing_stack and "does not exist" in stderr:
            return None
        detail = stderr.splitlines()[-1][:500] if stderr else "no error detail"
        raise CustomerStackError(f"AWS command failed: {detail}")
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def _caller_account_id(aws_cli: str) -> str:
    if shutil.which(aws_cli) is None:
        raise CustomerStackError(f"the AWS CLI {aws_cli!r} is not installed")
    document = _run_aws(aws_cli, ["sts", "get-caller-identity", "--output", "json"])
    try:
        return AwsCallerIdentity.model_validate(document).account_id
    except ValueError:
        raise CustomerStackError("ambient AWS identity returned no account ID") from None


def _stack_status(aws_cli: str, region: str, stack: str) -> tuple[str, str] | None:
    document = _run_aws(
        aws_cli,
        [
            "cloudformation",
            "describe-stacks",
            "--region",
            region,
            "--stack-name",
            stack,
            "--query",
            "Stacks[0].[StackStatus,StackStatusReason]",
            "--output",
            "json",
        ],
        allow_missing_stack=True,
    )
    if document is None:
        return None
    status, reason = StackStatusDocument.model_validate(document).root
    return status, reason or ""


def _wait_stack(
    aws_cli: str,
    region: str,
    stack: str,
    *,
    success: str,
    wait_states: frozenset[str],
    deadline: float,
    success_when_absent: bool,
) -> None:
    while True:
        described = _stack_status(aws_cli, region, stack)
        if described is None:
            if success_when_absent:
                return
            raise CustomerStackError(
                f"customer stack {stack} in {region} disappeared; "
                "its creation failed and rolled back"
            )
        status, reason = described
        if status == success:
            return
        if status not in wait_states:
            raise CustomerStackError(
                f"customer stack {stack} in {region} entered {status}: "
                f"{reason or 'no status reason'}"
            )
        if time.monotonic() >= deadline:
            raise CustomerStackError(
                f"timed out waiting on customer stack {stack} in {region}; last status {status}"
            )
        time.sleep(5)


def _apply(args: argparse.Namespace, deadline: float) -> dict[str, str]:
    action = AwsConnectionStackAction.model_validate_json(args.action_file.read_bytes())
    if action.account_id != args.account_id or action.region != args.region:
        raise CustomerStackError("stack creation request targets a different account or region")
    if action.template_sha256 != aws_account_connection_template_identity().sha256:
        raise CustomerStackError("stack creation request uses a different connection template")
    parameters = {item.ParameterKey: item.ParameterValue for item in action.request.Parameters}
    if parameters["PlatformPrincipalArn"] != args.platform_principal_arn:
        raise CustomerStackError("stack creation request targets a different platform principal")
    if _stack_status(args.aws_cli, args.region, action.request.StackName) is None:
        create: list[str] = [
            "cloudformation",
            "create-stack",
            "--region",
            args.region,
            "--output",
            "json",
        ]
        if args.execution_role_arn is not None:
            create.extend(["--role-arn", args.execution_role_arn])
        with tempfile.TemporaryDirectory(prefix="lazycloud-customer-stack-") as directory:
            request_file = Path(directory) / "request.json"
            request_file.write_text(action.request.model_dump_json(), encoding="utf-8")
            request_file.chmod(0o600)
            _run_aws(args.aws_cli, [*create, "--cli-input-json", f"file://{request_file}"])
    _wait_stack(
        args.aws_cli,
        args.region,
        action.request.StackName,
        success="CREATE_COMPLETE",
        wait_states=_CREATE_WAIT_STATES,
        deadline=deadline,
        success_when_absent=False,
    )
    return {
        "account_id": args.account_id,
        "action": "apply",
        "region": args.region,
        "stack_name": action.request.StackName,
        "status": "CREATE_COMPLETE",
    }


def _remove(args: argparse.Namespace, deadline: float) -> dict[str, str]:
    action = parse_aws_connection_stack_cleanup_action(
        args.action_url,
        account_id=args.account_id,
        region=args.region,
        expected_names=frozenset(args.stack_name),
    )
    if _stack_status(args.aws_cli, args.region, action.stack_id) is not None:
        delete: list[str] = [
            "cloudformation",
            "delete-stack",
            "--region",
            args.region,
            "--stack-name",
            action.stack_id,
        ]
        if args.execution_role_arn is not None:
            delete.extend(["--role-arn", args.execution_role_arn])
        _run_aws(args.aws_cli, delete)
        _wait_stack(
            args.aws_cli,
            args.region,
            action.stack_id,
            success="DELETE_COMPLETE",
            wait_states=_DELETE_WAIT_STATES,
            deadline=deadline,
            success_when_absent=True,
        )
    return {
        "account_id": args.account_id,
        "action": "remove",
        "region": args.region,
        "stack_name": action.name,
        "status": "DELETE_COMPLETE",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("apply", "Create the managed customer connection stack from a verified action."),
        ("remove", "Delete one exact managed customer connection stack."),
    ):
        subcommand = subcommands.add_parser(name, description=description)
        subcommand.add_argument("--account-id", required=True)
        subcommand.add_argument("--region", required=True)
        subcommand.add_argument("--execution-role-arn")
        subcommand.add_argument("--aws-cli", default="aws")
        subcommand.add_argument("--timeout", type=float, default=600)
    apply_command = subcommands.choices["apply"]
    apply_command.add_argument("--action-file", type=Path, required=True)
    apply_command.add_argument("--platform-principal-arn", required=True)
    remove_command = subcommands.choices["remove"]
    remove_command.add_argument("--action-url", required=True)
    remove_command.add_argument(
        "--stack-name",
        action="append",
        required=True,
        help="approved managed stack name; repeat for every candidate generation",
    )
    args = parser.parse_args(argv)
    if _ACCOUNT_ID.fullmatch(args.account_id) is None:
        parser.error("--account-id must contain exactly 12 digits")

    deadline = time.monotonic() + args.timeout
    try:
        ambient_account_id = _caller_account_id(args.aws_cli)
        if ambient_account_id != args.account_id:
            raise CustomerStackError("ambient AWS credentials target a different account")
        evidence = _apply(args, deadline) if args.command == "apply" else _remove(args, deadline)
    except (CustomerStackError, ValueError) as exc:
        print(f"customer stack {args.command} failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
