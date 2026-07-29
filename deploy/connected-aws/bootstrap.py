"""Provision the connected AWS control principal and print its configuration.

One command for a new deployment: it deploys `control-stack.yaml`, then prints the
exact `.env` lines its outputs map to. Nothing else in the repository creates the
control principal, so this is the only supported way to obtain it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from pydantic import TypeAdapter

_TEMPLATE = Path(__file__).with_name("control-stack.yaml")
_STACK_OUTPUTS = TypeAdapter(list[dict[str, str]])
_OUTPUT_ENV = {
    "ControlPrincipalArn": "LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN",
    "ControlStackName": "LAZYCLOUD_AWS_CONTROL_STACK_NAME",
    "OperatorRoleArn": "LAZYCLOUD_E2E_AWS_OPERATOR_ROLE_ARN",
    "CustomerStackExecutionRoleArn": "LAZYCLOUD_E2E_AWS_CUSTOMER_STACK_EXECUTION_ROLE_ARN",
}


class BootstrapError(RuntimeError):
    """The control stack could not be provisioned or read back."""


def _aws(command: list[str], *, aws_cli: str, region: str, profile: str | None) -> str:
    argv = [aws_cli, *command, "--region", region]
    if profile:
        argv.extend(["--profile", profile])
    # AWS CLI v1 rejects --no-cli-pager, so suppress the pager through the environment.
    environment = {**os.environ, "AWS_PAGER": ""}
    result = subprocess.run(argv, capture_output=True, text=True, env=environment, check=False)
    if result.returncode != 0:
        raise BootstrapError(f"{' '.join(command[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _caller_arn(*, aws_cli: str, region: str, profile: str | None) -> str:
    identity = _aws(
        ["sts", "get-caller-identity", "--output", "json"],
        aws_cli=aws_cli,
        region=region,
        profile=profile,
    )
    return str(json.loads(identity)["Arn"])


def _deploy(args: argparse.Namespace, trusted: list[str]) -> None:
    _aws(
        [
            "cloudformation",
            "deploy",
            "--stack-name",
            args.stack_name,
            "--template-file",
            str(_TEMPLATE),
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--parameter-overrides",
            f"TrustedPrincipalArns={','.join(trusted)}",
            f"CreateAcceptanceOperator={'true' if args.acceptance_operator else 'false'}",
            f"ControlRoleName={args.control_role_name}",
        ],
        aws_cli=args.aws_cli,
        region=args.region,
        profile=args.profile,
    )


def _outputs(args: argparse.Namespace) -> dict[str, str]:
    described = _aws(
        [
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            args.stack_name,
            "--query",
            "Stacks[0].Outputs",
            "--output",
            "json",
        ],
        aws_cli=args.aws_cli,
        region=args.region,
        profile=args.profile,
    )
    entries = _STACK_OUTPUTS.validate_json(described or "[]")
    return {
        entry["OutputKey"]: entry["OutputValue"]
        for entry in entries
        if "OutputKey" in entry and "OutputValue" in entry
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack-name", default="lazycloud-connected-aws-control")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or "us-east-1")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--aws-cli", default="aws")
    parser.add_argument("--control-role-name", default="lazycloud-compose-control")
    parser.add_argument(
        "--trusted-principal-arn",
        action="append",
        dest="trusted_principal_arns",
        help="principal permitted to assume the control role; repeatable. "
        "Defaults to the calling identity.",
    )
    parser.add_argument(
        "--acceptance-operator",
        action="store_true",
        help="also create the acceptance operator and customer-stack execution roles",
    )
    parser.add_argument(
        "--show-only",
        action="store_true",
        help="print the configuration of an existing stack without deploying",
    )
    args = parser.parse_args(argv)

    try:
        if not args.show_only:
            trusted = args.trusted_principal_arns or [
                _caller_arn(aws_cli=args.aws_cli, region=args.region, profile=args.profile)
            ]
            _deploy(args, trusted)
        outputs = _outputs(args)
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    missing = [key for key in ("ControlPrincipalArn", "ControlStackName") if key not in outputs]
    if missing:
        print(f"error: stack produced no {', '.join(missing)} output", file=sys.stderr)
        return 1

    print("# Add to .env:")
    for key, name in _OUTPUT_ENV.items():
        if key in outputs:
            print(f"{name}={outputs[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
