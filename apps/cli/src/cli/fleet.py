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

from typing import Annotated

import typer
from lazycloud.cli.components.output import console, json_output_enabled, print_payload
from lazycloud.cli.control import compute_client
from shared.aws_connections import AwsAccountConnectionPhase, AwsAccountNetwork

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
        # Restated on every deploy, not just the first: a connection made before
        # the platform could say which account was its own still describes itself
        # as a customer's, and its machines would serve nobody but us.
        adopted = client.adopt_fleet_account()
        if json_output_enabled(ctx):
            print_payload(ctx, adopted.model_dump(mode="json"))
            return
        console.print(f"AWS account {adopted.account_id} is already connected ({adopted.phase}).")
        if adopted.phase is not AwsAccountConnectionPhase.Ready:
            console.print("Run `cloud validate` to advance it.")
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
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(f"connected AWS account {account_id}; run `cloud validate` to activate it.")


__all__ = ["fleet_app"]
