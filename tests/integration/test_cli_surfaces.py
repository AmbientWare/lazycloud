from __future__ import annotations

import typer
from cli.main import build_admin_cli
from lazycloud.cli.main import build_public_cli
from typer._click.core import Command as TyperCommand
from typer.core import TyperGroup
from typer.main import get_command

public_cli = build_public_cli()
admin_cli = build_admin_cli()


def _command_paths(typer_app: typer.Typer) -> set[str]:
    root = get_command(typer_app)
    paths: set[str] = set()

    def walk(command: TyperCommand, prefix: tuple[str, ...] = ()) -> None:
        if not isinstance(command, TyperGroup):
            return
        for name, subcommand in sorted(command.commands.items()):
            path = (*prefix, name)
            paths.add(" ".join(path))
            walk(subcommand, path)

    walk(root)
    return paths


def _root_command_help(typer_app: typer.Typer) -> dict[str, str | None]:
    root = get_command(typer_app)
    if not isinstance(root, TyperGroup):
        return {}
    return {name: subcommand.help for name, subcommand in root.commands.items()}
