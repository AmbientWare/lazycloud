from __future__ import annotations

import glob
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated

import typer
from shared.http.volumes import DeletePathRequest, ListPathRequest, MovePathRequest

from lazycloud.abstractions.volume import Volume, VolumeOperationError
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.cli.control import volume_client

VOLUME_SCHEME = "lazycloud://"

volume_app = typer.Typer(help="Manage volumes.")


@volume_app.command("list")
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
            _format_bytes(item.size),
            item.workspace_name,
            item.updated_at.isoformat(),
        ]
        for item in response.volumes
    ]
    console.print(table("Volumes", ["name", "size", "workspace", "updated"], rows))


@volume_app.command("create")
def volume_create(
    ctx: typer.Context,
    name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = volume_client(workspace=workspace).create(name)
    if response.volume is None:
        raise typer.BadParameter("volume create failed")
    if json_output_enabled(ctx):
        print_payload(ctx, response.volume.model_dump(mode="json"))
        return
    console.print(
        table(
            "Volume",
            ["name", "workspace", "created"],
            [[response.volume.name, response.volume.workspace_name, response.volume.created_at]],
        )
    )


@volume_app.command("delete")
def volume_delete(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    if not yes:
        if not sys.stdin.isatty():
            raise ClientError(
                f"volume delete `{name}` requires confirmation, "
                "and no interactive terminal is attached",
                type="confirmation_required",
                title="Confirmation required",
                hint="Pass --yes to delete the volume and its files without a prompt.",
            )
        typer.confirm(
            "Deleting a volume also removes its files. Continue?",
            abort=True,
            err=True,
        )
    volume_client(workspace=workspace).delete(name)
    print_payload(ctx, {"name": name})


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
            "" if item.is_dir else _format_bytes(item.size),
            item.mod_time.isoformat(),
            "yes" if item.is_dir else "no",
        ]
        for item in response.path_infos
    ]
    output = table(
        f"{selected.full_path} ({len(response.path_infos)} items, {_format_bytes(total_size)})",
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
        print_payload(ctx, {"source": source_remote.full_path, "destination": str(result)})
        return
    selected_destination = destination_remote or parse_remote_path(destination)
    copied = _upload_to_remote(source, selected_destination, workspace=workspace)
    print_payload(
        ctx, {"source": source, "destination": selected_destination.full_path, "copied": copied}
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
    print_payload(ctx, {"deleted": list(response.deleted)})


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
    print_payload(ctx, {"new_path": response.new_path or new.full_path})


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


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{value} B"


__all__ = [
    "parse_remote_path",
    "parse_remote_path_if_schemed",
    "volume_app",
    "volume_cp",
    "volume_ls",
    "volume_mv",
    "volume_rm",
]
