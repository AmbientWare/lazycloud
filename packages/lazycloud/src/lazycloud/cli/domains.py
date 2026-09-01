from __future__ import annotations

from typing import Annotated

import typer
from shared.custom_domains import CustomDomainPhase
from shared.http.custom_domains import CustomDomainResponse

from lazycloud.cli.components.cards import notice_card, result_card
from lazycloud.cli.components.output import (
    console,
    emit,
    json_default,
    json_output_enabled,
    print_payload,
    table,
)
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
    summary: dict[str, object] = {
        "hostname": domain.hostname,
        "status": domain.phase.value,
    }
    if domain.verified_at is not None:
        summary["verified"] = domain.verified_at.isoformat()
    emit(
        ctx,
        payload=_payload(domain),
        view=result_card("Domain", json_default(summary)),
    )
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
                "Ownership records",
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
    emit(
        ctx,
        payload=_payload(registered),
        view=notice_card(
            "Domain added",
            f"{registered.hostname} is {registered.phase.value.replace('_', ' ')}.",
            hint=f"Run `lazycloud domain status {registered.hostname}` to check it.",
            tone="success" if registered.phase is CustomDomainPhase.Ready else "info",
        ),
    )
    if json_output_enabled(ctx):
        return
    if registered.error_message:
        console.print(notice_card("Domain error", registered.error_message, tone="warning"))
    if registered.phase is not CustomDomainPhase.Ready and registered.cname_target:
        _print_dns_record(registered)


@domain_app.command("list", help="List registered custom domains.")
def domain_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = domain_client(workspace=workspace).list()
    if json_output_enabled(ctx):
        print_payload(ctx, [_payload(item) for item in response.data])
        return
    rows = [[item.hostname, item.phase.value] for item in response.data]
    console.print(table("Domains", ["hostname", "status"], rows))


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
    emit(
        ctx,
        payload={"hostname": hostname, "removed": True},
        view=notice_card("Domain removed", f"Removed {hostname}.", tone="success"),
    )


__all__ = ["domain_app"]
