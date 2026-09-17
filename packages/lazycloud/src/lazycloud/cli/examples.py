from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from lazycloud.cli.components.cards import result_card
from lazycloud.cli.components.output import console, emit, json_output_enabled, print_payload, table
from lazycloud.example_catalog import example_catalog, write_projects

example_app = typer.Typer(help="List and download standalone example projects.")


@example_app.command("download", help="Write a bundled example project to disk.")
def example_download(
    ctx: typer.Context,
    name: str,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite example files.")] = False,
) -> None:
    catalog = example_catalog()
    target = output or Path("examples" if name == "all" else name)
    if name == "all":
        selected = {target / key: project for key, project in catalog.items()}
    else:
        if name not in catalog:
            raise typer.BadParameter(f"unknown example: {name}; run 'lazycloud example list'")
        selected = {target: catalog[name]}
    try:
        written = write_projects(selected, force=force)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    emit(
        ctx,
        payload={"name": name, "path": str(target), "files": written},
        view=result_card(
            {
                "path": str(target),
                "files": len(written),
                "next": (
                    "Choose a project directory and follow its README.md."
                    if name == "all"
                    else "Open the project directory and follow README.md."
                ),
            },
            title="Example downloaded",
            tone="success",
        ),
    )


@example_app.command("list", help="List example projects bundled with this SDK.")
def example_list(ctx: typer.Context) -> None:
    catalog = example_catalog()
    if json_output_enabled(ctx):
        print_payload(
            ctx,
            [
                {
                    "name": project.manifest.name,
                    "description": project.manifest.description,
                    "size_bytes": project.size_bytes,
                }
                for project in catalog.values()
            ],
        )
        return
    console.print(
        table(
            "Examples",
            ["name", "description"],
            [[project.manifest.name, project.manifest.description] for project in catalog.values()],
        )
    )


__all__ = ["example_app"]
