from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated

import typer
from shared.http.volumes import DeletePathRequest, ListPathRequest, MovePathRequest

from lazycloud.abstractions.volume import Volume, VolumeOperationError
from lazycloud.cli.components.cards import notice_card, result_card
from lazycloud.cli.components.formatting import bytes_count, timestamp
from lazycloud.cli.components.output import (
    console,
    emit,
    json_output_enabled,
    print_payload,
    table,
)
from lazycloud.cli.components.prompts import confirm_destructive
from lazycloud.cli.control import volume_client

VOLUME_SCHEME = "lazycloud://"

volume_app = typer.Typer(help="Manage volumes.")


@volume_app.command("list", help="List workspace volumes.")
def volume_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = volume_client(workspace=workspace).list_volumes()
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in response.volumes])
        return
    rows = [
        [
            item.name,
            bytes_count(item.size),
            timestamp(item.updated_at),
        ]
        for item in response.volumes
    ]
    console.print(table("Volumes", ["name", "size", "updated"], rows))


@volume_app.command("create", help="Create a volume.")
def volume_create(
    ctx: typer.Context,
    name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = volume_client(workspace=workspace).create(name)
    if response.volume is None:
        raise typer.BadParameter("volume create failed")
    emit(
        ctx,
        payload=response.volume.model_dump(mode="json"),
        view=notice_card(
            "Volume created",
            f"Created {response.volume.name}.",
            tone="success",
        ),
    )


@volume_app.command("delete", help="Delete a volume and its files.")
def volume_delete(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    confirm_destructive(
        ctx,
        subject=f"volume delete `{name}`",
        consequence="Deleting this volume also removes its files.",
        yes=yes,
    )
    volume_client(workspace=workspace).delete(name)
    emit(
        ctx,
        payload={"name": name},
        view=notice_card("Volume deleted", f"Deleted {name}.", tone="success"),
    )


def volume_ls(
    ctx: typer.Context,
    remote_path: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    selected = parse_remote_path(remote_path)
    response = volume_client(workspace=workspace).list_path(
        ListPathRequest(path=selected.full_path)
    )
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in response.path_infos])
        return
    total_size = sum(item.size for item in response.path_infos)
    rows = [
        [
            Path(item.path).name + ("/" if item.is_dir else ""),
            "" if item.is_dir else bytes_count(item.size),
            timestamp(item.mod_time),
            "yes" if item.is_dir else "no",
        ]
        for item in response.path_infos
    ]
    output = table(
        f"{selected.full_path} ({len(response.path_infos)} items, {bytes_count(total_size)})",
        ["name", "size", "modified", "directory"],
        rows,
    )
    console.print(output)


def volume_cp(
    ctx: typer.Context,
    source: str,
    destination: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    source_remote = parse_remote_path_if_schemed(source)
    destination_remote = parse_remote_path_if_schemed(destination)
    if source_remote and destination_remote:
        raise typer.BadParameter("source and destination cannot both be remote paths")
    if source_remote:
        target = _download_destination(source_remote, destination)
        result = Volume(source_remote.volume_name, workspace=workspace).get(
            source_remote.relative_path,
            target,
        )
        emit(
            ctx,
            payload={"source": source_remote.full_path, "destination": str(result)},
            view=result_card(
                "Download complete",
                {"saved_to": str(result)},
                tone="success",
            ),
        )
        return
    selected_destination = destination_remote or parse_remote_path(destination)
    copied = _upload_to_remote(source, selected_destination, workspace=workspace)
    emit(
        ctx,
        payload={"source": source, "destination": selected_destination.full_path, "copied": copied},
        view=result_card(
            "Upload complete",
            {"destination": selected_destination.full_path, "files": len(copied)},
            tone="success",
        ),
    )


def volume_rm(
    ctx: typer.Context,
    remote_path: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    selected = parse_remote_path(remote_path)
    response = volume_client(workspace=workspace).delete_path(
        DeletePathRequest(path=selected.full_path)
    )
    emit(
        ctx,
        payload={"deleted": list(response.deleted)},
        view=notice_card(
            "Paths deleted",
            f"Deleted {len(response.deleted)} path{'s' if len(response.deleted) != 1 else ''}.",
            tone="success",
        ),
    )


def volume_mv(
    ctx: typer.Context,
    original_path: str,
    new_path: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    original = parse_remote_path(original_path)
    new = parse_remote_path(new_path)
    if original.volume_name != new.volume_name:
        raise typer.BadParameter("volume mv requires paths in the same volume")
    response = volume_client(workspace=workspace).move_path(
        MovePathRequest(original_path=original.full_path, new_path=new.full_path)
    )
    emit(
        ctx,
        payload={"new_path": response.new_path or new.full_path},
        view=notice_card(
            "Path moved",
            f"Moved to {response.new_path or new.full_path}.",
            tone="success",
        ),
    )


@dataclass(frozen=True, slots=True)
class RemoteVolumePath:
    volume_name: str
    relative_path: str

    @property
    def full_path(self) -> str:
        if self.relative_path in {"", "."}:
            return self.volume_name
        return f"{self.volume_name}/{self.relative_path}"


def parse_remote_path(value: str) -> RemoteVolumePath:
    raw = value.removeprefix(VOLUME_SCHEME).lstrip("/")
    volume_name, separator, relative = raw.partition("/")
    if not volume_name:
        raise typer.BadParameter("remote volume path must include a volume name")
    if not separator:
        relative = "."
    return RemoteVolumePath(volume_name=volume_name, relative_path=_safe_relative_path(relative))


def parse_remote_path_if_schemed(value: str) -> RemoteVolumePath | None:
    if not value.startswith(VOLUME_SCHEME):
        return None
    return parse_remote_path(value)


def _upload_to_remote(
    source: str,
    destination: RemoteVolumePath,
    *,
    workspace: str | None,
) -> list[str]:
    matches = _local_matches(source)
    if not matches:
        raise typer.BadParameter(f"local path not found: {source}")
    copied: list[str] = []
    volume = Volume(destination.volume_name, workspace=workspace)
    for match in matches:
        target = _upload_destination(match, destination, multiple=len(matches) > 1)
        try:
            copied.append(volume.put(match, target))
        except VolumeOperationError as exc:
            raise typer.BadParameter(str(exc)) from exc
    return copied


def _local_matches(source: str) -> list[Path]:
    expanded = str(Path(source).expanduser())
    if glob.has_magic(expanded):
        return sorted(Path(item).resolve() for item in glob.glob(expanded, recursive=True))
    path = Path(expanded).resolve()
    return [path] if path.exists() else []


def _upload_destination(
    source: Path,
    destination: RemoteVolumePath,
    *,
    multiple: bool,
) -> str | None:
    if destination.relative_path == ".":
        return None if not multiple else source.name
    if multiple:
        return str(PurePosixPath(destination.relative_path, source.name))
    return destination.relative_path


def _download_destination(source: RemoteVolumePath, destination: str) -> Path:
    path = Path(destination).expanduser()
    if destination.endswith("/") or (path.exists() and path.is_dir()):
        return path / PurePosixPath(source.relative_path).name
    return path


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value or ".")
    if path.is_absolute() or ".." in path.parts:
        raise typer.BadParameter(f"unsafe volume path: {value}")
    return path.as_posix()


__all__ = [
    "parse_remote_path",
    "parse_remote_path_if_schemed",
    "volume_app",
    "volume_cp",
    "volume_ls",
    "volume_mv",
    "volume_rm",
]
