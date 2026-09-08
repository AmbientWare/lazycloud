"""Remove connected AWS through LazyCloud and corroborate scoped zero capacity.

The stage removes the public connection and applies any required customer cleanup
action through the deployment-owned operator command. It then checks workspace
resource tags in every managed region for remaining EC2, EBS and Auto Scaling capacity.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence

from lazycloud.cli.control import compute_client, control_config
from lazycloud.clients.compute.control import ComputeClient
from lazycloud.clients.workspace.control import WorkspaceControlClient
from shared.aws_connections import (
    AwsAccountConnectionAvailableAction,
    AwsAccountConnectionPhase,
)
from shared.http.aws_connections import AwsConnectionResponse
from shared.http.errors import HttpApiError
from tests.e2e.external import _support

_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_CUSTOMER_STACK_COMMAND = "deploy/connected-aws/customer_stack.py"


def _managed_stacks(connection: AwsConnectionResponse) -> dict[str, str]:
    """Return every managed stack name -> region across authorization generations."""
    stacks: dict[str, str] = {}
    for authorization in (
        connection.active_authorization,
        connection.pending_authorization,
        connection.retiring_authorization,
    ):
        managed = authorization.managed_authorization if authorization is not None else None
        if managed is not None:
            stacks[managed.stack_name] = managed.region
    return stacks


def _remove_customer_stacks(
    connection: AwsConnectionResponse,
    stacks: dict[str, str],
    args: argparse.Namespace,
    deadline: _support.Deadline,
) -> None:
    action = connection.customer_action
    if action is None or action.url is None or not stacks:
        raise RuntimeError(
            "AWS connection cleanup requires customer action but provides no automatable "
            f"stack target ({connection.phase}: {connection.detail or 'no detail'})"
        )
    regions = sorted(set(stacks.values()))
    if len(regions) != 1:
        raise RuntimeError(f"managed cleanup stacks span multiple regions: {regions}")
    command: list[str] = [
        sys.executable,
        _CUSTOMER_STACK_COMMAND,
        "remove",
        "--action-url",
        action.url,
        "--account-id",
        args.account_id,
        "--region",
        regions[0],
        "--timeout",
        str(deadline.remaining_seconds()),
    ]
    for stack_name in sorted(stacks):
        command.extend(["--stack-name", stack_name])
    if args.execution_role_arn is not None:
        command.extend(["--execution-role-arn", args.execution_role_arn])
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise RuntimeError("the operator customer-stack removal command failed")


def _wait_disconnected(
    client: ComputeClient,
    connection_id: str,
    stacks: dict[str, str],
    args: argparse.Namespace,
    deadline: _support.Deadline,
) -> None:
    removal_requested = False
    stacks_removed = False

    def check() -> bool | None:
        nonlocal removal_requested, stacks_removed
        connection = client.current_connection()
        instances = [
            {"id": item.id, "status": item.status, "region": item.region}
            for item in client.instances().data
            if item.provider == f"aws:{connection_id}"
        ]
        _support.emit_evidence(
            {
                "connection_id": connection_id,
                "phase": connection.phase if connection is not None else None,
                "instances": instances,
                "managed_stacks": stacks,
            }
        )
        if connection is None:
            return True
        if connection.id != connection_id or connection.account_id != args.account_id:
            raise RuntimeError("the AWS connection changed during cleanup")
        stacks.update(_managed_stacks(connection))
        if (
            not removal_requested
            and AwsAccountConnectionAvailableAction.Remove in connection.available_actions
        ):
            try:
                client.remove_account()
            except HttpApiError as exc:
                if exc.status_code not in {409, 503}:
                    raise
                _support.emit_evidence({"disconnect_retry_status": exc.status_code})
                return None
            removal_requested = True
            return None
        if connection.phase is AwsAccountConnectionPhase.ActionRequired and not stacks_removed:
            _remove_customer_stacks(connection, stacks, args, deadline)
            stacks_removed = True
        return None

    _support.poll_until(deadline, "the public AWS connection to disconnect", check)


def _corroborate_aws_zero(regions: Sequence[str], workspace_id: str) -> None:
    filters = [
        "Name=tag:cloud-pool:managed-by,Values=control-plane",
        f"Name=tag:cloud-pool:workspace,Values={workspace_id}",
    ]
    for region in regions:
        instances = _support.run_aws(
            [
                "ec2",
                "describe-instances",
                "--region",
                region,
                "--filters",
                *filters,
                "Name=instance-state-name,Values=pending,running,stopping,stopped",
                "--query",
                "Reservations[].Instances[].InstanceId",
                "--output",
                "json",
            ]
        )
        volumes = _support.run_aws(
            [
                "ec2",
                "describe-volumes",
                "--region",
                region,
                "--filters",
                *filters,
                "--query",
                "Volumes[].VolumeId",
                "--output",
                "json",
            ]
        )
        groups = _support.run_aws(
            [
                "autoscaling",
                "describe-auto-scaling-groups",
                "--region",
                region,
                "--query",
                (
                    "AutoScalingGroups[?"
                    "Tags[?Key=='cloud-pool:managed-by' && Value=='control-plane'] && "
                    f"Tags[?Key=='cloud-pool:workspace' && Value=='{workspace_id}']"
                    "].[AutoScalingGroupName,DesiredCapacity]"
                ),
                "--output",
                "json",
            ]
        )
        if instances or volumes or groups:
            raise RuntimeError(
                f"AWS region {region} still reports LazyCloud capacity for the workspace"
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--execution-role-arn")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)
    if _ACCOUNT_ID.fullmatch(args.account_id) is None:
        parser.error("--account-id must contain exactly 12 digits")
    deadline = _support.Deadline(args.timeout)

    client = compute_client(timeout_seconds=30)
    try:
        ambient_account_id = _support.ambient_aws_account_id()
        config = control_config(timeout_seconds=30)
        workspace = _support.first_public_call(
            WorkspaceControlClient.from_endpoint(
                config.endpoint,
                token=config.token,
                timeout_seconds=30,
                workspace=config.workspace,
            ).current
        )
        connection = _support.first_public_call(client.current_connection)
        catalog = _support.first_public_call(client.catalog)
    except _support.MissingPrerequisite as exc:
        return _support.skip("AWS cleanup", exc)
    if ambient_account_id != args.account_id:
        raise RuntimeError("ambient AWS credentials target a different account")
    if connection is not None and connection.account_id != args.account_id:
        raise RuntimeError("the workspace is connected to a different AWS account")

    stacks = _managed_stacks(connection) if connection is not None else {}
    regions = {
        *(item.region for item in catalog.data if item.provider == "aws"),
        *stacks.values(),
        *(
            item.region
            for item in client.instances().data
            if connection is not None and item.provider == f"aws:{connection.id}"
        ),
    }
    if not regions:
        raise RuntimeError("no managed AWS region is available to verify scoped cleanup")
    if connection is not None:
        _wait_disconnected(client, connection.id, stacks, args, deadline)

    checked_regions = sorted(regions | set(stacks.values()))
    _corroborate_aws_zero(checked_regions, workspace.id)
    _support.emit_evidence(
        {
            "account_id": args.account_id,
            "capability": "cloud.disconnect.aws",
            "connection": None,
            "hourly_micros": 0,
            "instances": 0,
            "regions": checked_regions,
            "workspace_id": workspace.id,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
