from __future__ import annotations

from typing import Annotated

import typer
from shared.custom_domains import CustomDomainPhase
from shared.http.custom_domains import CustomDomainResponse

from lazycloud.cli.components.cards import notice_card
from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.cli.control import domain_client

domain_app = typer.Typer(help="Manage the domains this workspace can serve from.")


def _payload(domain: CustomDomainResponse) -> dict[str, object]:
    return {
        "hostname": domain.hostname,
        "phase": domain.phase.value,
        "cname_target": domain.cname_target,
        "required_records": [r.model_dump() for r in domain.required_records],
        "error": domain.error_message,
        "verified_at": domain.verified_at.isoformat() if domain.verified_at else None,
    }


def _print(ctx: typer.Context, domain: CustomDomainResponse) -> None:
    print_payload(ctx, _payload(domain), title="Domain")
    if json_output_enabled(ctx):
        return
    if domain.error_message:
        console.print(notice_card("Domain error", domain.error_message, tone="warning"))
    if domain.phase is not CustomDomainPhase.Ready and domain.cname_target:
        _print_dns_record(domain)


def _print_dns_record(domain: CustomDomainResponse) -> None:
    """Print the record to create, named the way a DNS form asks for it."""

    console.print(
        table(
            "DNS record",
            ["type", "name", "target"],
            [["CNAME", domain.hostname, domain.cname_target]],
        )
    )
    if domain.required_records:
        console.print(
            table(
                "Also add these, to prove you own the domain",
                ["type", "name", "value"],
                [[r.type, r.name, r.value] for r in domain.required_records],
            )
        )


@domain_app.command("add")
def domain_add(
    ctx: typer.Context,
    domain: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Register a domain, then point it at the platform with the CNAME shown."""

    registered = domain_client(workspace=workspace).register(domain)
    _print(ctx, registered)
    if not json_output_enabled(ctx):
        console.print(
            notice_card(
                "Next step",
                "Wait for DNS to propagate, then check the domain status.",
                hint=f"Run lazycloud domain status {registered.hostname}",
            )
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
    rows = [[item.hostname, item.phase.value, item.cname_target or "-"] for item in response.data]
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
    print_payload(
        ctx,
        {"hostname": hostname, "removed": True},
        title="Domain removed",
        tone="success",
    )


__all__ = ["domain_app"]
