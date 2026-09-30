from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import JsonValue

from lazycloud._terminal.cards import notice_card, result_card
from lazycloud._terminal.formatting import timestamp
from lazycloud._terminal.streams import console
from lazycloud.cli.components.output import emit, json_output_enabled, print_payload, write_stream
from lazycloud.cli.components.results import emit_python_result
from lazycloud.session.task import Task

task_app = typer.Typer(help="Inspect and manage tasks.")

_OUTPUT_OPTION = typer.Option(
    "--output",
    help="Save the result to a .png, .html, .txt, .json, or .pkl file.",
    dir_okay=False,
    writable=True,
)


@task_app.command("show", help="Show one task and its current state.")
def task_show(
    ctx: typer.Context,
    task_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    view = Task.from_id(task_id, workspace=workspace).status()
    summary: dict[str, JsonValue] = {
        "function": f"{view.app}.{view.function}",
        "status": view.status.value,
        "attempts": view.attempts,
        "requested": timestamp(view.created_at),
    }
    if view.started_at is not None:
        summary["started"] = timestamp(view.started_at)
    if view.finished_at is not None:
        summary["finished"] = timestamp(view.finished_at)
    if view.failure is not None:
        label = f"{view.failure.type}: " if view.failure.type else ""
        summary["failure"] = f"{view.failure.kind.value} · {label}{view.failure.message}"
    emit(ctx, payload=view.model_dump(mode="json"), view=result_card(summary))


@task_app.command("result", help="Wait for a task and display its result.")
def task_result(
    ctx: typer.Context,
    task_id: str,
    timeout_seconds: Annotated[float | None, typer.Option("--timeout", min=0)] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    output: Annotated[Path | None, _OUTPUT_OPTION] = None,
) -> None:
    task = Task.from_id(task_id, workspace=workspace)
    if json_output_enabled(ctx):
        value = task.result(timeout_seconds=timeout_seconds)
    else:
        with console.status(f"Waiting for task {task_id}…"):
            value = task.result(timeout_seconds=timeout_seconds)
    emit_python_result(ctx, value, output=output)


@task_app.command("logs", help="Print a task's output.")
def task_logs(
    ctx: typer.Context,
    task_id: str,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep printing until the task finishes.")
    ] = False,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    entries = Task.from_id(task_id, workspace=workspace).logs(follow=follow)
    if json_output_enabled(ctx):
        print_payload(ctx, [entry.model_dump(mode="json") for entry in entries])
        return
    for entry in entries:
        data = entry.data if entry.data.endswith("\n") else f"{entry.data}\n"
        write_stream(data, error=entry.stream.value != "stdout")


@task_app.command("cancel", help="Cancel a queued or running task.")
def task_cancel(
    ctx: typer.Context,
    task_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    view = Task.from_id(task_id, workspace=workspace).cancel()
    emit(
        ctx,
        payload=view.model_dump(mode="json"),
        view=notice_card(f"Task {task_id} is {view.status.value}.", tone="success"),
    )


__all__ = ["task_app"]
