from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table

example_app = typer.Typer(help="Manage example apps.")


QUICKSTART_TEMPLATE = """from __future__ import annotations

from lazycloud import App, Image

app = App("quickstart")
image = Image(python_version="3.12")


@app.function(name="hello", image=image, cpu=1.0, memory="256Mi")
def hello(name: str = "world") -> str:
    print(f"quickstart greeting for {name}", flush=True)
    return f"hello {name}"


if __name__ == "__main__":
    print(hello.local("lazycloud"))
"""

TASK_QUEUE_TEMPLATE = """from __future__ import annotations

from lazycloud import App, Image

app = App("task_queue_example")
image = Image(python_version="3.12")


@app.task_queue(name="summaries", image=image, workers=1)
def summarize(values: list[int]) -> dict[str, int]:
    return {"count": len(values), "total": sum(values)}


if __name__ == "__main__":
    print(summarize.local([1, 2, 3]))
"""


@dataclass(frozen=True, slots=True)
class ExampleTemplate:
    name: str
    description: str
    files: dict[str, str]

    @property
    def size_bytes(self) -> int:
        return sum(len(content.encode("utf-8")) for content in self.files.values())


TEMPLATES: dict[str, ExampleTemplate] = {
    "quickstart": ExampleTemplate(
        name="quickstart",
        description="Minimal function app.",
        files={
            "quickstart.py": QUICKSTART_TEMPLATE,
            "README.md": "# Quickstart\n\nRun locally with `python quickstart.py`.\n",
        },
    ),
    "task-queue": ExampleTemplate(
        name="task-queue",
        description="Task queue app.",
        files={
            "task_queue.py": TASK_QUEUE_TEMPLATE,
            "README.md": "# Task Queue\n\nRun locally with `python task_queue.py`.\n",
        },
    ),
}


def quickstart(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("quickstart.py"),
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files.")] = False,
) -> None:
    _write_file(output, QUICKSTART_TEMPLATE, force=force)
    print_payload(ctx, {"path": str(output), "written": True})


def create_app(
    ctx: typer.Context,
    name: str,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files.")] = False,
) -> None:
    target = output or Path(name)
    written = _write_template(name, target, force=force)
    print_payload(ctx, {"name": name, "path": str(target), "files": written})


@example_app.command("download")
def example_download(
    ctx: typer.Context,
    name: str,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files.")] = False,
) -> None:
    if name == "all":
        base = output or Path("examples")
        files: dict[str, list[str]] = {}
        for template_name in TEMPLATES:
            files[template_name] = _write_template(
                template_name,
                base / template_name,
                force=force,
            )
        print_payload(ctx, {"name": name, "path": str(base), "files": files})
        return
    target = output or Path(name)
    written = _write_template(name, target, force=force)
    print_payload(ctx, {"name": name, "path": str(target), "files": written})


@example_app.command("list")
def example_list(ctx: typer.Context) -> None:
    rows = [
        [template.name, template.description, _format_bytes(template.size_bytes)]
        for template in TEMPLATES.values()
    ]
    if json_output_enabled(ctx):
        print_payload(
            ctx,
            [
                {
                    "name": template.name,
                    "description": template.description,
                    "size_bytes": template.size_bytes,
                }
                for template in TEMPLATES.values()
            ],
        )
        return
    console.print(table("Examples", ["name", "description", "size"], rows))


def _write_template(name: str, target: Path, *, force: bool) -> list[str]:
    template = TEMPLATES.get(name)
    if template is None:
        raise typer.BadParameter(f"unknown example: {name}")
    written: list[str] = []
    for relative_path, content in template.files.items():
        destination = target / relative_path
        _write_file(destination, content, force=force)
        written.append(str(destination))
    return written


def _write_file(path: Path, content: str, *, force: bool) -> None:
    selected = path.expanduser()
    if selected.exists() and not force:
        raise typer.BadParameter(f"file already exists: {selected}")
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(content, encoding="utf-8")


def _format_bytes(value: int) -> str:
    return f"{value} B"


__all__ = ["create_app", "example_app", "quickstart"]
