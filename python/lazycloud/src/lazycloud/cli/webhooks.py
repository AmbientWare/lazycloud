from __future__ import annotations

from typing import Annotated
from uuid import UUID

import typer

from lazycloud._terminal.cards import notice_card, result_card
from lazycloud._terminal.formatting import timestamp
from lazycloud._terminal.streams import console
from lazycloud.cli.components.output import (
    emit,
    json_output_enabled,
    print_payload,
    table,
)
from lazycloud.clients.api import ApiClient
from lazycloud.contracts.api import Webhook, WebhookEvent
from lazycloud.control import api_client, require_workspace, resolve_control_client_config

webhook_app = typer.Typer(help="Manage webhooks that receive task and deploy events.")

ALL_APPS = "all apps"


def _session(workspace: str | None) -> tuple[ApiClient, str]:
    config = resolve_control_client_config(workspace=workspace)
    return api_client(config), require_workspace(config)


@webhook_app.command("list", help="List webhooks and the events they receive.")
def webhook_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = _session(workspace)
    webhooks: list[Webhook] = []
    cursor: str | None = None
    while True:
        page = client.list_webhooks(selected, cursor=cursor, limit=100)
        webhooks.extend(page.webhooks)
        if not page.next_cursor:
            break
        cursor = page.next_cursor
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in webhooks])
        return
    rows: list[list[object]] = [
        [
            str(item.id),
            item.url,
            item.app or ALL_APPS,
            ", ".join(event.value for event in item.events),
            item.enabled,
        ]
        for item in webhooks
    ]
    console.print(table("Webhooks", ["id", "url", "app", "events", "enabled"], rows))


@webhook_app.command("add", help="Add a webhook that receives the given events.")
def webhook_add(
    ctx: typer.Context,
    url: str,
    events: Annotated[
        list[WebhookEvent],
        typer.Option("--event", help="An event to send; repeat for several."),
    ],
    app: Annotated[
        str | None, typer.Option("--app", help="Only send events from this app.")
    ] = None,
    description: Annotated[str | None, typer.Option("--description")] = None,
    disabled: Annotated[
        bool, typer.Option("--disabled", help="Create it without sending events.")
    ] = False,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = _session(workspace)
    webhook = client.create_webhook(
        selected,
        url,
        events,
        app=app,
        description=description,
        enabled=False if disabled else None,
    )
    emit(
        ctx,
        payload=webhook.model_dump(mode="json"),
        view=notice_card(f"Added webhook {webhook.id}.", tone="success"),
    )


@webhook_app.command("update", help="Change a webhook's URL, events, app, or state.")
def webhook_update(
    ctx: typer.Context,
    webhook: UUID,
    url: Annotated[str | None, typer.Option("--url")] = None,
    events: Annotated[
        list[WebhookEvent] | None,
        typer.Option("--event", help="Replace the events; repeat for several."),
    ] = None,
    app: Annotated[
        str | None, typer.Option("--app", help="Only send events from this app.")
    ] = None,
    all_apps: Annotated[
        bool, typer.Option("--all-apps", help="Send events from every app.")
    ] = False,
    description: Annotated[str | None, typer.Option("--description")] = None,
    enabled: Annotated[bool | None, typer.Option("--enable/--disable")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    if app is not None and all_apps:
        raise typer.BadParameter("choose only one of --app or --all-apps")
    client, selected = _session(workspace)
    updated = client.update_webhook(
        selected,
        webhook,
        url=url,
        app="" if all_apps else app,
        events=events or None,
        description=description,
        enabled=enabled,
    )
    emit(
        ctx,
        payload=updated.model_dump(mode="json"),
        view=notice_card(f"Updated webhook {updated.id}.", tone="success"),
    )


@webhook_app.command("remove", help="Remove a webhook.")
def webhook_remove(
    ctx: typer.Context,
    webhook: UUID,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = _session(workspace)
    client.delete_webhook(selected, webhook)
    emit(
        ctx,
        payload={"id": str(webhook), "deleted": True},
        view=notice_card(f"Removed webhook {webhook}.", tone="success"),
    )


@webhook_app.command("test", help="Send a webhook.test event to a webhook.")
def webhook_test(
    ctx: typer.Context,
    webhook: UUID,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = _session(workspace)
    delivery = client.test_webhook(selected, webhook)
    emit(
        ctx,
        payload=delivery.model_dump(mode="json"),
        view=result_card(
            {"delivery": delivery.id, "state": delivery.state.value},
            message="Queued a test delivery; `deliveries` shows how it went.",
        ),
    )


@webhook_app.command("deliveries", help="List a webhook's recent deliveries, newest first.")
def webhook_deliveries(
    ctx: typer.Context,
    webhook: UUID,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 50,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected = _session(workspace)
    deliveries = client.list_webhook_deliveries(selected, webhook, limit=limit).deliveries
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in deliveries])
        return
    rows: list[list[object]] = [
        [
            timestamp(item.created_at),
            item.event,
            item.state,
            item.attempts,
            item.response_status,
            item.last_error,
        ]
        for item in deliveries
    ]
    console.print(
        table(
            "Deliveries",
            ["created", "event", "state", "attempts", "status", "last error"],
            rows,
        )
    )


__all__ = ["webhook_app"]
