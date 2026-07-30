from __future__ import annotations

from typing import Annotated

import typer
from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.cli.workspaces import workspace_audit, workspace_rename
from lazycloud.json_contracts import JsonValue, validate_json_object
from shared.app_identity import DEFAULT_RESOURCE_TYPE
from shared.deployments import StubKind
from shared.http.concurrency import ConcurrencyLimitSetRequest
from shared.http.source_cache_cleanup import SourceCacheCleanupStatusResponse
from shared.http.stubs import StubCreateRequest
from shared.http.workspaces import WorkspaceResponse, WorkspaceSetRequest, WorkspaceStorageResponse

from cli.api_client import admin_api_client
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
    print_payload(ctx, _workspace_payload(record))


@workspace_app.command("list")
def workspace_list(
    ctx: typer.Context,
    include_deleted: Annotated[bool, typer.Option("--all")] = False,
) -> None:
    records = admin_api_client().list_workspaces(include_deleted=include_deleted).workspaces
    if json_output_enabled(ctx):
        print_payload(ctx, [_workspace_payload(item) for item in records])
        return
    rows = [[item.id, item.name, item.status.value, item.storage.backend] for item in records]
    console.print(table("Workspaces", ["id", "name", "status", "storage"], rows))


@workspace_app.command("show")
def workspace_show(
    ctx: typer.Context,
    workspace_id_or_name: Annotated[str, typer.Argument()] = "default",
) -> None:
    record = admin_api_client().get_workspace(workspace_id_or_name)
    print_payload(ctx, _workspace_payload(record))


@workspace_app.command("cleanup-status")
def workspace_cleanup_status(
    ctx: typer.Context,
    workspace_id_or_name: Annotated[str, typer.Argument()] = "default",
) -> None:
    record = admin_api_client().get_source_cache_cleanup_status(workspace_id_or_name)
    if json_output_enabled(ctx):
        print_payload(ctx, _source_cache_cleanup_payload(record))
        return
    oldest_age = (
        str(record.oldest_pending_age_seconds)
        if record.oldest_pending_age_seconds is not None
        else "-"
    )
    console.print(
        table(
            "Source Cache Cleanup",
            [
                "workspace",
                "pending",
                "claimed",
                "completed",
                "generations",
                "failing",
                "error",
                "oldest (s)",
                "complete",
            ],
            [
                [
                    record.workspace_id,
                    record.pending_count,
                    record.claimed_count,
                    record.completed_count,
                    record.generations_pending,
                    record.failing_count,
                    record.last_error_code.value if record.last_error_code else "-",
                    oldest_age,
                    record.complete,
                ]
            ],
        )
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
    print_payload(ctx, record.model_dump(mode="json"))


@stub_app.command("list")
def stub_list(
    ctx: typer.Context,
    workspace_name: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    records = admin_api_client(workspace_name).list_stubs().stubs
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
        return
    rows = [[item.id, item.workspace_id, item.name, item.kind.value] for item in records]
    console.print(table("Stubs", ["id", "workspace", "name", "kind"], rows))


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
    print_payload(ctx, record.model_dump(mode="json"))


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
            item.id,
            item.workspace_id,
            item.name,
            str(item.in_flight),
            str(item.limit),
            str(item.available),
        ]
        for item in records
    ]
    console.print(
        table(
            "Concurrency Limits",
            ["id", "workspace", "name", "used", "limit", "free"],
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
    print_payload(ctx, result.model_dump(mode="json"))


@concurrency_app.command("release")
def concurrency_release(
    ctx: typer.Context,
    limit_id_or_name: str,
    workspace_name: Annotated[str, typer.Option("--workspace")] = "default",
) -> None:
    result = admin_api_client(workspace_name).release_concurrency(limit_id_or_name)
    print_payload(ctx, result.model_dump(mode="json"))
