from __future__ import annotations

from typing import Annotated

import typer
from lazycloud.cli.components.formatting import duration
from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.cli.workspaces import workspace_audit, workspace_rename
from lazycloud.json_contracts import JsonValue, validate_json_object
from shared.app_identity import DEFAULT_RESOURCE_TYPE
from shared.deployments import StubKind
from shared.http.concurrency import ConcurrencyLimitSetRequest
from shared.http.source_cache_cleanup import SourceCacheCleanupStatusResponse
from shared.http.stubs import StubCreateRequest
from shared.http.workspaces import (
    WorkspaceCreateRequest,
    WorkspaceResponse,
    WorkspaceSetRequest,
    WorkspaceStorageResponse,
)

from cli.api_client import admin_api_client
from cli.components.results import emit_result
from cli.parameters import parse_key_values

workspace_app = typer.Typer(help="Manage workspaces.")
stub_app = typer.Typer(help="Manage deployed stubs.")
concurrency_app = typer.Typer(help="Manage concurrency limits.")


def _parse_metadata(values: list[str]) -> dict[str, JsonValue]:
    return {key: value for key, value in parse_key_values(values).items()}


def _workspace_payload(record: WorkspaceResponse) -> dict[str, JsonValue]:
    return validate_json_object(record.model_dump(mode="json"))


def _source_cache_cleanup_payload(
    record: SourceCacheCleanupStatusResponse,
) -> dict[str, JsonValue]:
    return validate_json_object(record.model_dump(mode="json"))


def _show_workspace(ctx: typer.Context, record: WorkspaceResponse, *, title: str) -> None:
    emit_result(
        ctx,
        payload=_workspace_payload(record),
        title=title,
        fields={
            "name": record.name,
            "status": record.status.value,
            "storage": record.storage.backend,
            "bucket": record.storage.bucket,
            "id": record.id,
        },
        tone="success" if title != "Workspace" else "neutral",
    )


@workspace_app.command("create")
def workspace_create(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument()] = None,
) -> None:
    """Create a workspace along with the storage it needs to run anything.

    `set` records a name and a storage document; this is what actually creates the
    bucket that document describes, so a workspace made with `set` alone accepts work
    and then cannot run it.
    """

    record = admin_api_client().create_workspace(WorkspaceCreateRequest(name=name))
    _show_workspace(ctx, record, title="Workspace created")


@workspace_app.command("set")
def workspace_set(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument()] = "default",
    storage_backend: Annotated[str, typer.Option("--storage-backend")] = "local",
    storage_bucket: Annotated[str | None, typer.Option("--storage-bucket")] = None,
    storage_prefix: Annotated[str, typer.Option("--storage-prefix")] = "",
    signing_key_prefix: Annotated[str | None, typer.Option("--signing-key-prefix")] = None,
    primary_token_id: Annotated[str | None, typer.Option("--primary-token-id")] = None,
    labels: Annotated[
        list[str] | None,
        typer.Option("--label", help="Workspace label as KEY=VALUE."),
    ] = None,
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", help="Workspace metadata as KEY=VALUE."),
    ] = None,
) -> None:
    record = admin_api_client().upsert_workspace(
        name,
        WorkspaceSetRequest(
            name=name,
            storage=WorkspaceStorageResponse(
                backend=storage_backend,
                bucket=storage_bucket,
                prefix=storage_prefix,
            ),
            signing_key_prefix=signing_key_prefix,
            primary_token_id=primary_token_id,
            labels=parse_key_values(labels or []),
            metadata=_parse_metadata(metadata or []),
        ),
    )
    _show_workspace(ctx, record, title="Workspace saved")


@workspace_app.command("list")
def workspace_list(
    ctx: typer.Context,
    include_deleted: Annotated[bool, typer.Option("--all")] = False,
) -> None:
    records = admin_api_client().list_workspaces(include_deleted=include_deleted).workspaces
    if json_output_enabled(ctx):
        print_payload(ctx, [_workspace_payload(item) for item in records])
        return
    rows = [[item.name, item.status.value, item.storage.backend, item.id] for item in records]
    console.print(table("Workspaces", ["name", "status", "storage", "id"], rows))


@workspace_app.command("show")
def workspace_show(
    ctx: typer.Context,
    workspace_id_or_name: Annotated[str, typer.Argument()] = "default",
) -> None:
    record = admin_api_client().get_workspace(workspace_id_or_name)
    _show_workspace(ctx, record, title="Workspace")


@workspace_app.command("cleanup-status")
def workspace_cleanup_status(
    ctx: typer.Context,
    workspace_id_or_name: Annotated[str, typer.Argument()] = "default",
) -> None:
    record = admin_api_client().get_source_cache_cleanup_status(workspace_id_or_name)
    if json_output_enabled(ctx):
        print_payload(ctx, _source_cache_cleanup_payload(record))
        return
    emit_result(
        ctx,
        payload=_source_cache_cleanup_payload(record),
        title="Source cache cleanup",
        fields={
            "complete": record.complete,
            "pending": record.pending_count,
            "claimed": record.claimed_count,
            "failing": record.failing_count,
            "oldest pending": (
                duration(record.oldest_pending_age_seconds)
                if record.oldest_pending_age_seconds is not None
                else "none"
            ),
            "error": record.last_error_code.value if record.last_error_code else "none",
        },
        tone="success" if record.complete else "warning",
    )


workspace_app.command("rename")(workspace_rename)
workspace_app.command("audit")(workspace_audit)


@stub_app.command("create")
def stub_create(
    ctx: typer.Context,
    name: str,
    workspace_name: Annotated[str, typer.Option("--workspace")] = "default",
    kind: Annotated[StubKind, typer.Option("--kind")] = StubKind.Function,
    handler: Annotated[str | None, typer.Option("--handler")] = None,
    deployment_id: Annotated[str | None, typer.Option("--deployment-id")] = None,
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", help="Stub metadata as KEY=VALUE."),
    ] = None,
) -> None:
    record = admin_api_client(workspace_name).create_stub(
        StubCreateRequest(
            name=name,
            workspace=workspace_name,
            kind=kind,
            handler=handler,
            deployment_id=deployment_id,
            metadata=_parse_metadata(metadata or []),
        )
    )
    emit_result(
        ctx,
        payload=record.model_dump(mode="json"),
        title="Workload stub created",
        fields={
            "name": record.name,
            "kind": record.kind.value,
            "workspace": record.workspace_id,
            "deployment": record.deployment_id,
            "id": record.id,
        },
        tone="success",
    )


@stub_app.command("list")
def stub_list(
    ctx: typer.Context,
    workspace_name: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    records = admin_api_client(workspace_name).list_stubs().stubs
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
        return
    rows = [[item.name, item.kind.value, item.workspace_id, item.id] for item in records]
    console.print(table("Stubs", ["name", "kind", "workspace", "id"], rows))


@concurrency_app.command("set")
def concurrency_set(
    ctx: typer.Context,
    name: str,
    limit: int,
    workspace_name: Annotated[str, typer.Option("--workspace")] = "default",
    resource_type: Annotated[str, typer.Option("--resource-type")] = DEFAULT_RESOURCE_TYPE,
    resource_id: Annotated[str | None, typer.Option("--resource-id")] = None,
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", help="Limit metadata as KEY=VALUE."),
    ] = None,
) -> None:
    record = admin_api_client(workspace_name).set_concurrency_limit(
        ConcurrencyLimitSetRequest(
            name=name,
            workspace=workspace_name,
            limit=limit,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=_parse_metadata(metadata or []),
        )
    )
    emit_result(
        ctx,
        payload=record.model_dump(mode="json"),
        title="Concurrency limit saved",
        fields={
            "name": record.name,
            "used": record.in_flight,
            "limit": record.limit,
            "free": record.available,
            "id": record.id,
        },
        tone="success",
    )


@concurrency_app.command("list")
def concurrency_list(
    ctx: typer.Context,
    workspace_name: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    records = admin_api_client(workspace_name).list_concurrency_limits().limits
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
        return
    rows = [
        [
            item.name,
            str(item.in_flight),
            str(item.limit),
            str(item.available),
            item.workspace_id,
            item.id,
        ]
        for item in records
    ]
    console.print(
        table(
            "Concurrency limits",
            ["name", "used", "limit", "free", "workspace", "id"],
            rows,
        )
    )


@concurrency_app.command("acquire")
def concurrency_acquire(
    ctx: typer.Context,
    limit_id_or_name: str,
    workspace_name: Annotated[str, typer.Option("--workspace")] = "default",
) -> None:
    result = admin_api_client(workspace_name).acquire_concurrency(limit_id_or_name)
    emit_result(
        ctx,
        payload=result.model_dump(mode="json"),
        title="Concurrency acquired" if result.acquired else "Concurrency unavailable",
        fields={
            "name": result.record.name,
            "acquired": result.acquired,
            "free": result.available_after,
            "reason": result.reason,
        },
        tone="success" if result.acquired else "warning",
    )


@concurrency_app.command("release")
def concurrency_release(
    ctx: typer.Context,
    limit_id_or_name: str,
    workspace_name: Annotated[str, typer.Option("--workspace")] = "default",
) -> None:
    result = admin_api_client(workspace_name).release_concurrency(limit_id_or_name)
    emit_result(
        ctx,
        payload=result.model_dump(mode="json"),
        title="Concurrency released",
        fields={
            "name": result.record.name,
            "free": result.available_after,
        },
        tone="success",
    )
