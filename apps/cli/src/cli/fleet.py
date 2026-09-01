"""Register the capacity the platform provisions in its own AWS account.

The shared fleet is a connection like a customer's, because the code has exactly
one way to say "an account we may provision in": `provider_machines` refuses to
launch unless a pool carries a `provider_connection_id`, and even an internal
unit is created with one. Ours differs only in that Terraform builds the network
and the role rather than a stack the customer deploys.

That leaves one durable record to create, which is why this exists as a command
rather than a runbook step. A deploy runs it every time, so the fleet is
registered by the same thing that ships the release rather than by whoever
remembers.
"""

from __future__ import annotations

import time
from typing import Annotated

import typer
from lazycloud.cli.components.cards import result_card
from lazycloud.cli.components.output import emit
from lazycloud.cli.components.results import emit_result
from lazycloud.cli.control import compute_client, workspace_client
from lazycloud.json_contracts import validate_json_object
from shared.aws_connections import AwsAccountConnectionPhase, AwsAccountNetwork
from shared.contracts import ContractModel
from shared.http.compute import UnitResponse
from shared.http.errors import HttpApiError

from cli.api_client import admin_api_client

fleet_app = typer.Typer(help="Register the platform's own compute capacity.")


@fleet_app.command("ensure")
def fleet_ensure(
    ctx: typer.Context,
    account_id: Annotated[str, typer.Option("--account-id", help="AWS account ID.")],
    role_arn: Annotated[str, typer.Option("--role-arn", help="Connection role to assume.")],
    vpc_id: Annotated[str, typer.Option("--vpc-id", help="VPC pools launch into.")],
    subnet_id: Annotated[
        list[str],
        typer.Option("--subnet-id", help="Subnet to launch into. Exactly two, in two zones."),
    ],
    security_group_id: Annotated[
        str, typer.Option("--security-group-id", help="Security group nodes join.")
    ],
    external_id: Annotated[
        str,
        typer.Option(
            "--external-id",
            help="External ID the connection role already enforces.",
            envvar="LAZYCLOUD_FLEET_EXTERNAL_ID",
        ),
    ],
) -> None:
    """Connect the platform's own account, or report the connection already there.

    Idempotent because a deploy runs it on every release. A connection already
    present is left alone rather than replaced: reconnecting mints a new
    authorization generation, and doing that on every deploy would churn the
    credential every pool depends on.

    This is the ordinary existing-role connection, not a private path: the role
    exists before the connection does, so its external ID is supplied rather
    than minted. A customer bringing their own role is in exactly that position.
    """
    if len(subnet_id) != 2:
        raise typer.BadParameter(
            f"exactly two --subnet-id are required, in different availability zones; "
            f"got {len(subnet_id)}"
        )

    client = compute_client()

    existing = client.current_connection()
    if existing is not None:
        # Restated on every deploy. A connection made before the platform could
        # say which account was its own still describes itself
        # as a customer's, and its machines would serve nobody but us.
        adopted = client.adopt_fleet_account()
        emit_result(
            ctx,
            payload=adopted.model_dump(mode="json"),
            title="Fleet account connected",
            fields={
                "account": adopted.account_id,
                "phase": adopted.phase.value,
                "detail": adopted.detail,
            },
            tone="success" if adopted.phase is AwsAccountConnectionPhase.Ready else "info",
            message=(
                "Run `cloud validate` to advance the connection."
                if adopted.phase is not AwsAccountConnectionPhase.Ready
                else ""
            ),
        )
        return

    response = client.connect_account(
        account_id=account_id,
        role_arn=role_arn,
        # The role is declared beside this deployment and its trust already
        # enforces this, so the platform is told rather than choosing. A minted
        # one would have to be written into a trust policy Terraform owns, by
        # something other than Terraform.
        external_id=external_id,
        network=AwsAccountNetwork(
            vpc_id=vpc_id,
            subnet_ids=(subnet_id[0], subnet_id[1]),
            security_group_id=security_group_id,
        ),
        # This account is the platform's, so its machines are shared capacity that
        # serves every customer and bills to the fleet.
        platform_fleet=True,
    )
    connection = response.connection
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Fleet account connected",
        fields={
            "account": connection.account_id,
            "phase": connection.phase.value,
            "detail": connection.detail,
        },
        tone="success",
        message="Run `cloud validate` to activate the connection.",
    )


__all__ = ["fleet_app"]


class FleetUnitTeardown(ContractModel):
    """What became of one unit, whether or not it went.

    Reported per unit rather than as a single verdict: one contended lease must
    not hide the units that did go, and an operator finishing this by hand needs
    to know which ones are left and why.
    """

    workspace_id: str
    unit_id: str
    name: str
    deleted: bool
    attempts: int
    reason: str = ""


FLEET_DESTROY_ATTEMPT_INTERVAL_SECONDS = 10.0
FLEET_DESTROY_TIMEOUT_SECONDS = 600.0
"""How long one unit is given to finish being deleted.

Longer than the capacity mutation lease, which is what a delete contends with.
That lease is held for up to five minutes and renewed by whoever holds it, so a
budget shorter than it turns a scheduler that happens to be mid-reconcile into
a teardown that reports failure and leaves the fleet running.
"""

_RETRYABLE_CODES = frozenset({"capacity_reservation_lock_contended", "upstream_unavailable"})
"""Answers that mean "not yet", as opposed to "no".

The lease is transient by construction. The upstream failure is the provider
teardown reporting that it is partway through: deleting an autoscaling group
returns before the group is gone, so the first delete tears the group down and
the next one, once AWS has finished, removes the launch template and succeeds.
Reading either as a refusal is what leaves capacity running.
"""


@fleet_app.command("destroy")
def fleet_destroy(
    ctx: typer.Context,
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout", help="How long to give each unit before reporting it."),
    ] = FLEET_DESTROY_TIMEOUT_SECONDS,
    interval_seconds: Annotated[
        float,
        typer.Option("--interval", help="How long to wait between attempts."),
    ] = FLEET_DESTROY_ATTEMPT_INTERVAL_SECONDS,
) -> None:
    """Delete every provisioning unit, so nothing is left to launch machines.

    The counterpart to `ensure`, and the step before `terraform destroy`. A
    unit's autoscaling group and launch template are created at runtime, so
    Terraform has never heard of them: destroy the cluster with a unit still
    standing and its group keeps launching instances with nothing alive to stop
    it.

    Deleting the unit is what removes them, and a successful delete is the proof.
    The route answers 204 only once the provider reported the group and the
    launch template both gone, so there is nothing further to check and no need
    to read AWS to check it.

    What this does not do is delete an autoscaling group itself. A group whose
    unit is gone is named in the output and left alone: this cannot prove an
    unclaimed resource is one the platform made, and an operator can. Reaching
    past the control plane to delete one is what recreated two of them the last
    time this was done by hand. The units still existed, so the scheduler rebuilt
    their groups minutes later.
    """
    if timeout_seconds <= 0:
        raise typer.BadParameter("--timeout must be greater than zero")
    if interval_seconds <= 0:
        raise typer.BadParameter("--interval must be greater than zero")

    outcomes = [
        _destroy_unit(
            workspace_id=workspace_id,
            unit_id=unit.id,
            unit_name=unit.name,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )
        for workspace_id, unit in _every_unit()
    ]
    remaining = [outcome for outcome in outcomes if not outcome.deleted]
    payload = validate_json_object(
        {
            "units": [outcome.model_dump(mode="json") for outcome in outcomes],
            "remaining": len(remaining),
        }
    )
    emit(
        ctx,
        payload=payload,
        view=result_card(
            "Fleet units removed" if not remaining else "Fleet teardown incomplete",
            {
                "removed": sum(outcome.deleted for outcome in outcomes),
                "remaining": len(remaining),
                "failures": [
                    f"{outcome.name}: {outcome.reason or 'not removed'}" for outcome in remaining
                ],
            },
            tone="success" if not remaining else "warning",
        ),
    )
    if remaining:
        # Named, and the command fails. A teardown that reported success with a
        # unit still standing is the one outcome worth preventing, because the
        # next step destroys the cluster that would have stopped it.
        raise typer.Exit(code=1)


def _every_unit() -> list[tuple[str, UnitResponse]]:
    """Every unit in every workspace, with the workspace that addresses it.

    Both halves are needed: a unit is looked up within one workspace, so an id
    on its own reaches nothing. Fanned out here because the route is
    workspace-scoped and a teardown that reads one workspace leaves the rest
    provisioning.
    """
    found: list[tuple[str, UnitResponse]] = []
    for workspace in workspace_client().list().workspaces:
        for unit in admin_api_client(workspace.id).list_units().pools:
            found.append((workspace.id, unit))
    return found


def _destroy_unit(
    *,
    workspace_id: str,
    unit_id: str,
    unit_name: str,
    timeout_seconds: float,
    interval_seconds: float,
) -> FleetUnitTeardown:
    client = admin_api_client(workspace_id)
    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    reason = ""
    while True:
        attempts += 1
        try:
            client.delete_unit(unit_id)
        except HttpApiError as error:
            if error.status_code == 404:
                # Already gone, which is the state this is trying to reach.
                return _outcome(workspace_id, unit_id, unit_name, True, attempts, "already absent")
            reason = error.code or str(error.status_code)
            if error.code not in _RETRYABLE_CODES:
                return _outcome(workspace_id, unit_id, unit_name, False, attempts, reason)
        else:
            return _outcome(workspace_id, unit_id, unit_name, True, attempts, "")
        if time.monotonic() + interval_seconds >= deadline:
            return _outcome(workspace_id, unit_id, unit_name, False, attempts, reason)
        time.sleep(interval_seconds)


def _outcome(
    workspace_id: str,
    unit_id: str,
    unit_name: str,
    deleted: bool,
    attempts: int,
    reason: str,
) -> FleetUnitTeardown:
    return FleetUnitTeardown(
        workspace_id=workspace_id,
        unit_id=unit_id,
        name=unit_name,
        deleted=deleted,
        attempts=attempts,
        reason=reason,
    )
