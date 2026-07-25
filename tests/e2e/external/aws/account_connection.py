"""Connect the ambient AWS account through LazyCloud's public customer flow.

The stage drives only the public connection API; the CloudFormation customer
action is applied by the deployment-owned operator command, which validates the
action against the exact verified release template and platform principal.
Run from the repository root:

``uv run --env-file .env python -m tests.e2e.external.aws.account_connection
--account-id <id> --template-url <url> --platform-principal-arn <arn>``
"""

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
from collections.abc import Sequence

from lazycloud.cli.control import compute_client
from lazycloud.clients.compute.control import ComputeClient
from shared.aws_connections import AwsAccountConnectionPhase
from shared.http.aws_connections import AwsConnectionResponse
from shared.http.errors import HttpTransportError
from tests.e2e.external import _support

_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_CUSTOMER_STACK_COMMAND = "deploy/connected-aws/customer_stack.py"


def _managed_region(connection: AwsConnectionResponse) -> str:
    authorization = connection.pending_authorization or connection.active_authorization
    managed = authorization.managed_authorization if authorization is not None else None
    if managed is None:
        raise RuntimeError("the AWS connection has no managed authorization region")
    return managed.region


def _apply_customer_action(
    action_url: str,
    args: argparse.Namespace,
    region: str,
    deadline: _support.Deadline,
) -> None:
    command: list[str] = [
        sys.executable,
        _CUSTOMER_STACK_COMMAND,
        "apply",
        "--action-url",
        action_url,
        "--account-id",
        args.account_id,
        "--region",
        region,
        "--template-url",
        args.template_url,
        "--platform-principal-arn",
        args.platform_principal_arn,
        "--timeout",
        str(deadline.remaining_seconds()),
    ]
    if args.execution_role_arn is not None:
        command.extend(["--execution-role-arn", args.execution_role_arn])
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise RuntimeError("the operator customer-stack command failed")


def _wait_ready(
    client: ComputeClient,
    deadline: _support.Deadline,
    *,
    revalidation_attempts: int = 0,
) -> AwsConnectionResponse:
    """Wait for the public connection to become Ready before the stage deadline.

    ``revalidation_attempts`` bounds how many times a Degraded or ActionRequired
    phase is answered with another ``validate_connection`` call instead of a
    terminal failure; IAM role visibility immediately after customer stack
    creation is eventually consistent.
    """
    attempts_left = revalidation_attempts

    def check() -> AwsConnectionResponse | None:
        nonlocal attempts_left
        connection = client.current_connection()
        if connection is None:
            raise RuntimeError("the public AWS connection disappeared while becoming ready")
        if connection.phase is AwsAccountConnectionPhase.Ready:
            return connection
        if connection.phase in {
            AwsAccountConnectionPhase.Degraded,
            AwsAccountConnectionPhase.ActionRequired,
        }:
            if attempts_left <= 0:
                raise RuntimeError(
                    f"AWS connection validation failed "
                    f"({connection.phase}: {connection.detail or 'no detail'})"
                )
            attempts_left -= 1
            with contextlib.suppress(HttpTransportError):
                client.validate_connection()
        return None

    return _support.poll_until(deadline, "the public AWS connection to become ready", check)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--template-url", required=True)
    parser.add_argument("--platform-principal-arn", required=True)
    parser.add_argument("--execution-role-arn")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    if _ACCOUNT_ID.fullmatch(args.account_id) is None:
        parser.error("--account-id must contain exactly 12 digits")
    deadline = _support.Deadline(args.timeout)

    client = compute_client(timeout_seconds=30)
    try:
        ambient_account_id = _support.ambient_aws_account_id()
        current = _support.first_public_call(client.current_connection)
    except _support.MissingPrerequisite as exc:
        return _support.skip("AWS connection", exc)
    if ambient_account_id != args.account_id:
        raise RuntimeError("ambient AWS credentials target a different account")
    if current is not None and current.account_id != args.account_id:
        raise RuntimeError("the workspace is connected to a different AWS account")

    region = ""
    stack_applied = False
    if current is None:
        response = client.connect_account(account_id=args.account_id)
        current = response.connection
        action_url = response.authorization.url
    elif current.phase is AwsAccountConnectionPhase.Degraded:
        response = client.reconnect_account()
        current = response.connection
        action_url = response.authorization.url
    else:
        action_url = current.customer_action.url if current.customer_action is not None else None

    if current.phase is not AwsAccountConnectionPhase.Ready:
        if action_url is not None:
            region = _managed_region(current)
            _apply_customer_action(action_url, args, region, deadline)
            client.validate_connection()
            stack_applied = True
            ready = _wait_ready(client, deadline, revalidation_attempts=5)
        else:
            # Validating or RetiringAuthorization: the platform converges on
            # its own; Degraded/ActionRequired without an action fails closed.
            ready = _wait_ready(client, deadline)
    else:
        ready = current

    _support.emit_evidence(
        {
            "account_id": ready.account_id,
            "capability": "cloud.connect.aws",
            "connection_id": ready.id,
            "customer_stack_applied": stack_applied,
            "phase": ready.phase,
            "region": region or None,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
