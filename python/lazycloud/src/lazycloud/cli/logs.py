from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from functools import partial
from itertools import islice
from typing import Annotated
from uuid import UUID

import typer

from lazycloud._terminal.cards import empty_state
from lazycloud._terminal.streams import console
from lazycloud.cli.components.output import (
    json_output_enabled,
    print_json_line,
    print_payload,
    write_stream,
)
from lazycloud.cli.control import api_session
from lazycloud.contracts.api import LogEntry
from lazycloud.session.deployment import resolve_deployment
from lazycloud.session.task import follow_log_stream


def logs(
    ctx: typer.Context,
    deployment: Annotated[
        str | None,
        typer.Option("--deployment", help="Deployment name or ID."),
    ] = None,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    container_id: Annotated[str | None, typer.Option("--container-id")] = None,
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", min=1, max=1000, help="Display the last N lines."),
    ] = 250,
    show_timestamp: Annotated[
        bool,
        typer.Option("--show-timestamp", help="Include log timestamps."),
    ] = False,
    follow: Annotated[
        bool,
        typer.Option("--follow", "-f", help="Follow new logs."),
    ] = False,
    max_events: Annotated[
        int,
        typer.Option("--max-events", min=0, help="Stop after N streamed log events."),
    ] = 0,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    if sum(item is not None for item in (deployment, task_id, container_id)) != 1:
        msg = "supply exactly one of --deployment, --task-id, or --container-id"
        raise typer.BadParameter(msg)
    client, selected_workspace = api_session(workspace=workspace)
    open_stream: Callable[..., Iterator[LogEntry]]
    if deployment is not None:
        found = resolve_deployment(client, selected_workspace, deployment).deployment
        open_stream = partial(
            client.stream_workload_logs, selected_workspace, found.app, found.kind, found.name
        )
    elif task_id is not None:
        open_stream = partial(
            client.stream_task_logs, selected_workspace, _uuid(task_id, "--task-id")
        )
    else:
        assert container_id is not None
        open_stream = partial(
            client.stream_container_logs, selected_workspace, _uuid(container_id, "--container-id")
        )
    if follow:
        # A reopened stream continues after the last entry rather than the tail.
        entries = follow_log_stream(
            lambda after: open_stream(after=after, tail=None if after else lines, follow=True)
        )
        for entry in islice(entries, max_events or None):
            if json_output_enabled(ctx):
                print_json_line(entry.model_dump(mode="json"), file=sys.stdout)
            else:
                _write_entry(entry, show_timestamp=show_timestamp)
        return
    stored = list(open_stream(tail=lines))
    if json_output_enabled(ctx):
        print_payload(ctx, [entry.model_dump(mode="json") for entry in stored])
        return
    if not stored:
        console.print(empty_state("No log entries found."))
        return
    for entry in stored:
        _write_entry(entry, show_timestamp=show_timestamp)


def _write_entry(entry: LogEntry, *, show_timestamp: bool) -> None:
    line = f"[{entry.time.isoformat()}] {entry.data}" if show_timestamp else entry.data
    write_stream(line if line.endswith("\n") else f"{line}\n")


def _uuid(value: str, option: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise typer.BadParameter(f"{option} must be an id, not {value!r}") from None


__all__ = ["logs"]
