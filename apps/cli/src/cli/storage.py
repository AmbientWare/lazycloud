from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from lazycloud.cli.components.formatting import bytes_count
from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.cli.components.results import emit_notice, emit_result
from pydantic import JsonValue
from shared.bytes_transport import encode_bytes
from shared.http.storage import CacheCreateRequest, ObjectCreateRequest

from cli.api_client import admin_api_client

object_app = typer.Typer(help="Manage object storage records.")
cache_app = typer.Typer(help="Manage file cache entries.")


@object_app.command("put")
def object_put(
    ctx: typer.Context,
    bucket: str,
    key: str,
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    record = admin_api_client().create_object(
        ObjectCreateRequest(
            bucket=bucket,
            key=key,
            value_base64=encode_bytes(source.read_bytes()),
        )
    )
    emit_result(
        ctx,
        payload=record.model_dump(mode="json"),
        title="Object uploaded",
        fields={
            "object": f"{record.bucket}/{record.key}",
            "size": bytes_count(record.size),
        },
        tone="success",
    )


@object_app.command("list")
def object_list(
    ctx: typer.Context,
    bucket: Annotated[str | None, typer.Argument()] = None,
    prefix: Annotated[str, typer.Option("--prefix")] = "",
) -> None:
    records = admin_api_client().list_objects(bucket=bucket, prefix=prefix).objects
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
    else:
        rows = [[item.bucket, item.key, bytes_count(item.size)] for item in records]
        console.print(table("Objects", ["bucket", "key", "size"], rows))


@object_app.command("read")
def object_read(
    ctx: typer.Context,
    bucket: str,
    key: str,
    output: Annotated[Path | None, typer.Option("--output", "-o", dir_okay=False)] = None,
) -> None:
    response = admin_api_client().read_object(bucket, key)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    data = response.bytes_value()
    if output is None:
        stream = typer.get_binary_stream("stdout")
        stream.write(data)
        stream.flush()
        return
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    console.print(str(destination), highlight=False, markup=False, soft_wrap=True)


@object_app.command("delete")
def object_delete(ctx: typer.Context, bucket: str, key: str) -> None:
    admin_api_client().delete_object(bucket, key)
    payload: dict[str, JsonValue] = {"bucket": bucket, "key": key, "deleted": True}
    emit_notice(
        ctx,
        payload=payload,
        title="Object deleted",
        message=f"Deleted {bucket}/{key}.",
    )


@cache_app.command("put")
def cache_put(
    ctx: typer.Context,
    namespace: str,
    key: str,
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    record = admin_api_client().create_cache_entry(
        CacheCreateRequest(
            namespace=namespace,
            key=key,
            value_base64=encode_bytes(source.read_bytes()),
        )
    )
    emit_result(
        ctx,
        payload=record.model_dump(mode="json"),
        title="Cache entry uploaded",
        fields={"entry": f"{namespace}/{key}", "size": bytes_count(record.size)},
        tone="success",
    )


@cache_app.command("list")
def cache_list(ctx: typer.Context) -> None:
    records = admin_api_client().list_cache().entries
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
    else:
        emit_result(
            ctx,
            payload=[item.model_dump(mode="json") for item in records],
            title="Cache",
            fields={
                "entries": len(records),
                "size": bytes_count(sum(item.size for item in records)),
                "hits": sum(item.hits for item in records),
            },
        )


@cache_app.command("get")
def cache_get(
    ctx: typer.Context,
    namespace: str,
    key: str,
    destination: Annotated[Path, typer.Argument(dir_okay=False)],
) -> None:
    response = admin_api_client().read_cache_entry(namespace, key)
    target = destination.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.bytes_value())
    if json_output_enabled(ctx):
        print_payload(
            ctx,
            {**response.entry.model_dump(mode="json"), "destination": str(target)},
        )
        return
    console.print(str(target), highlight=False, markup=False, soft_wrap=True)


@cache_app.command("delete")
def cache_delete(ctx: typer.Context, namespace: str, key: str) -> None:
    admin_api_client().delete_cache_entry(namespace, key)
    payload: dict[str, JsonValue] = {"namespace": namespace, "key": key, "deleted": True}
    emit_notice(
        ctx,
        payload=payload,
        title="Cache entry deleted",
        message=f"Deleted {namespace}/{key}.",
    )
