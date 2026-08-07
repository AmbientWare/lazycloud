from __future__ import annotations

from typing import Annotated

import typer
from shared.http.custom_domains import CustomDomainResponse

from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.cli.control import domain_client

domain_app = typer.Typer(help="Manage the domains this workspace can serve from.")


def _payload(domain: CustomDomainResponse) -> dict[str, object]:
    return {
        "hostname": domain.hostname,
        "phase": domain.phase.value,
        "verification_target": domain.verification_target,
        "error": domain.error_message,
        "verified_at": domain.verified_at.isoformat() if domain.verified_at else None,
    }


def _print(ctx: typer.Context, domain: CustomDomainResponse) -> None:
    if json_output_enabled(ctx):
        print_payload(ctx, _payload(domain))
        return
    console.print(
        table(
            "Domain",
            ["hostname", "status", "cname target"],
            [[domain.hostname, domain.phase.value, domain.verification_target or "-"]],
        )
    )
    if domain.error_message:
        console.print(f"[red]{domain.error_message}[/red]")


@domain_app.command("add")
def domain_add(
    ctx: typer.Context,
    domain: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Register a domain, then point it at the platform with the CNAME shown."""

    registered = domain_client(workspace=workspace).register(domain)
    _print(ctx, registered)
    if not json_output_enabled(ctx) and registered.verification_target:
        console.print(
            f"\nAdd a CNAME for [bold]{registered.hostname}[/bold] pointing at "
            f"[bold]{registered.verification_target}[/bold], then run "
            f"`lazycloud domain status {registered.hostname}`."
        )


@domain_app.command("list")
def domain_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = domain_client(workspace=workspace).list()
    if json_output_enabled(ctx):
        print_payload(ctx, [_payload(item) for item in response.data])
        return
    rows = [
        [item.hostname, item.phase.value, item.verification_target or "-"] for item in response.data
    ]
    console.print(table("Domains", ["hostname", "status", "cname target"], rows))


@domain_app.command("status")
def domain_status(
    ctx: typer.Context,
    hostname: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Show one domain as the edge currently reports it.

    A single read, not a wait: certificate issuance follows the customer's DNS and
    printing a spinner against it would only hide how long that takes.
    """

    _print(ctx, domain_client(workspace=workspace).get(hostname))


@domain_app.command("remove")
def domain_remove(
    ctx: typer.Context,
    hostname: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Retire a domain, discarding its certificate."""

    domain_client(workspace=workspace).remove(hostname)
    if json_output_enabled(ctx):
        print_payload(ctx, {"hostname": hostname, "removed": True})
        return
    console.print(f"Removed {hostname}.")


__all__ = ["domain_app"]
