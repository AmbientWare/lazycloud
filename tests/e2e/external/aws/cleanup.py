"""Remove connected AWS through LazyCloud and corroborate scoped zero capacity.

The stage zeroes the workspace compute policy, removes the public connection,
applies any required customer cleanup action through the deployment-owned
operator command, and then corroborates — read-only, tag-scoped — that every
region capacity could have launched in reports zero LazyCloud EC2, EBS, and
Auto Scaling capacity for the workspace.
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
from shared.compute_policy import AwsWorkspaceComputePolicy
from shared.http.aws_connections import AwsConnectionResponse
from shared.http.compute_policy import WorkspaceComputePolicyUpdateRequest
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


def _zero_policy(
    client: ComputeClient,
    deadline: _support.Deadline,
) -> AwsWorkspaceComputePolicy:
    """Zero the workspace policy, waiting out an in-flight reconcile.

    The owner rejects a policy write while it is reconciling capacity, which is
    exactly when a cleanup runs. Treating that as terminal aborts the stage and
    leaves the capacity it was asked to release still running.
    """

    def attempt() -> AwsWorkspaceComputePolicy | None:
        try:
            return _apply_zero_policy(client)
        except HttpApiError as exc:
            if exc.status_code in {409, 503}:
                return None
            raise

    return _support.poll_until(deadline, "the workspace compute policy to zero", attempt)


def _apply_zero_policy(client: ComputeClient) -> AwsWorkspaceComputePolicy:
    current = client.policy()
    aws = current.aws
    zero = AwsWorkspaceComputePolicy(
        default_region=aws.default_region,
        default_instance_type=aws.default_instance_type,
        initial_cpu_workers=0,
        min_cpu_workers=0,
        max_cpu_instances=0,
        max_gpu_instances=0,
        min_free_cpu_millicores=0,
        min_free_memory_mib=0,
        allowed_regions=aws.allowed_regions,
        allowed_instance_types=aws.allowed_instance_types,
        idle_timeout_seconds=aws.idle_timeout_seconds,
        root_volume_gib=aws.root_volume_gib,
    )
    # Zero under the group the workspace already has. Flipping to another
    # first would release capacity through a different branch than the one a user
    # takes, and zeroing the policy is the only control they are given.
    if current.aws != zero:
        client.update_policy(
            WorkspaceComputePolicyUpdateRequest(
                expected_revision=current.revision,
                default_pool=current.default_pool,
                aws=zero,
            )
        )
    return zero


def _wait_public_zero(client: ComputeClient, deadline: _support.Deadline) -> None:
    def check() -> bool | None:
        summary = client.summary()
        converged = (
            summary.instances.total == 0
            and summary.instances.ready == 0
            and summary.instances.pending == 0
            and summary.instances.degraded == 0
            and summary.cost.hourly_micros == 0
        )
        return True if converged else None

    _support.poll_until(deadline, "public compute to reach zero AWS capacity", check)


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
    stacks: dict[str, str],
    args: argparse.Namespace,
    deadline: _support.Deadline,
) -> None:
    removal_requested = False
    stacks_removed = False

    def check() -> bool | None:
        nonlocal removal_requested, stacks_removed
        connection = client.current_connection()
        if connection is None:
            return True
        stacks.update(_managed_stacks(connection))
        if (
            not removal_requested
            and AwsAccountConnectionAvailableAction.Remove in connection.available_actions
        ):
            client.remove_account()
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
    except _support.MissingPrerequisite as exc:
        return _support.skip("AWS cleanup", exc)
    if ambient_account_id != args.account_id:
        raise RuntimeError("ambient AWS credentials target a different account")
    if connection is not None and connection.account_id != args.account_id:
        raise RuntimeError("the workspace is connected to a different AWS account")

    stacks = _managed_stacks(connection) if connection is not None else {}
    zero = _zero_policy(client, deadline)
    _wait_public_zero(client, deadline)
    if connection is not None:
        _wait_disconnected(client, stacks, args, deadline)

    regions = sorted({zero.default_region, *zero.allowed_regions, *stacks.values()})
    _corroborate_aws_zero(regions, workspace.id)
    _support.emit_evidence(
        {
            "account_id": args.account_id,
            "capability": "cloud.disconnect.aws",
            "connection": None,
            "hourly_micros": 0,
            "instances": 0,
            "regions": regions,
            "workspace_id": workspace.id,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
