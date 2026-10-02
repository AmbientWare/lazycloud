from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from pydantic import JsonValue

from lazycloud._terminal.cards import empty_state, notice_card, result_card
from lazycloud._terminal.formatting import timestamp
from lazycloud._terminal.streams import console
from lazycloud.cli.apps import app_name
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import (
    emit,
    json_default,
    json_output_enabled,
    print_payload,
    table,
    write_stream,
)
from lazycloud.cli.components.results import emit_python_result
from lazycloud.cli.control import api_session
from lazycloud.contracts.api import Task as TaskView
from lazycloud.session.task import TERMINAL_STATUSES, Task, decode_payload

task_app = typer.Typer(help="Inspect and manage tasks.")

_OUTPUT_OPTION = typer.Option(
    "--output",
    help="Save the result to a .png, .html, .txt, .json, or .pkl file.",
    dir_okay=False,
    writable=True,
)


@task_app.command("list", help="List recent tasks.")
def task_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 100,
    app: Annotated[str | None, typer.Option("--app", help="App name or ID.")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected_workspace = api_session(workspace=workspace)
    selected_app = app_name(client, selected_workspace, app) if app else None
    tasks: list[TaskView] = []
    cursor: str | None = None
    while len(tasks) < limit:
        page = client.list_tasks(
            selected_workspace, app=selected_app, limit=min(1000, limit - len(tasks)), cursor=cursor
        )
        tasks.extend(page.tasks)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in tasks])
        return
    rows: list[list[object]] = [
        [item.function, item.status.value, timestamp(item.created_at), str(item.id)]
        for item in tasks
    ]
    console.print(table("Tasks", ["workload", "status", "requested", "id"], rows))


@task_app.command("stop", help="Stop one or more tasks.")
def task_stop(
    ctx: typer.Context,
    task_ids: Annotated[list[str], typer.Argument()],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected_workspace = api_session(workspace=workspace)
    response = client.stop_tasks(selected_workspace, [_task_uuid(item) for item in task_ids])
    summary: dict[str, JsonValue] = {"stopped": len(response.stopped)}
    if response.skipped:
        summary["skipped"] = [str(item) for item in response.skipped]
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(
            summary,
            title="Tasks stopped",
            tone="warning" if response.skipped else "success",
        ),
    )


@task_app.command("show", help="Show one task and its current state.")
def task_show(
    ctx: typer.Context,
    task_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    task = Task.from_id(task_id, workspace=workspace).view()
    summary: dict[str, JsonValue] = {
        "workload": task.function,
        "status": task.status.value,
        "requested": timestamp(task.created_at),
    }
    if task.max_attempts > 1:
        summary["attempt"] = f"{max(task.attempts, 1)} of {task.max_attempts}"
    if task.started_at is not None:
        summary["started"] = timestamp(task.started_at)
    if task.finished_at is not None:
        summary["finished"] = timestamp(task.finished_at)
    if task.container_id is not None:
        summary["container"] = str(task.container_id)
    if task.failure is not None:
        failure = task.failure
        summary["error"] = f"{failure.type}: {failure.message}" if failure.type else failure.message
    emit(ctx, payload=task.model_dump(mode="json"), view=result_card(json_default(summary)))


@task_app.command("result", help="Wait for and display a task result.")
def task_result(
    ctx: typer.Context,
    task_id: str,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
    timeout_seconds: Annotated[float | None, typer.Option("--timeout", min=0)] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    output: Annotated[Path | None, _OUTPUT_OPTION] = None,
) -> None:
    task = Task.from_id(task_id, workspace=workspace)
    if wait and not json_output_enabled(ctx):
        with console.status(f"Waiting for task {task_id}…"):
            result = task.result(wait=True, timeout_seconds=timeout_seconds)
    else:
        result = task.result(wait=wait, timeout_seconds=timeout_seconds)
    if result.ok and result.value is not None:
        emit_python_result(ctx, decode_payload(result.value), output=output)
        return
    if result.status in TERMINAL_STATUSES:
        raise ClientError(
            result.error or f"task {task_id} finished with status {result.status.value}",
            type="task_failed",
            title="Task failed",
        )
    emit(
        ctx,
        payload=result.task.model_dump(mode="json"),
        view=notice_card(
            f"Task {task_id} is {result.status.value}.",
            hint=f"Run `lazycloud task result {task_id}` to wait for it.",
        ),
    )


@task_app.command("logs", help="Print logs for one task.")
def task_logs(
    ctx: typer.Context,
    task_id: str,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 250,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    entries = Task.from_id(task_id, workspace=workspace).logs(limit=limit)
    if json_output_enabled(ctx):
        print_payload(ctx, [entry.model_dump(mode="json") for entry in entries])
        return
    if not entries:
        console.print(empty_state("No log entries found."))
        return
    for entry in entries:
        write_stream(entry.data if entry.data.endswith("\n") else f"{entry.data}\n")


@task_app.command("cancel", help="Cancel a task.")
def task_cancel(
    ctx: typer.Context,
    task_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    view = Task.from_id(task_id, workspace=workspace).cancel()
    emit(
        ctx,
        payload=view.model_dump(mode="json"),
        view=notice_card(f"Cancelled task {task_id}.", tone="success"),
    )


def _task_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise typer.BadParameter(f"not a task id: {value}") from None


__all__ = ["task_app"]
