from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeAlias

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)

ProgressCallback: TypeAlias = Callable[[int], None]


@dataclass
class Terminal:
    quiet: bool = False

    def write(self, message: str) -> None:
        if not self.quiet:
            sys.stdout.write(message)
            sys.stdout.flush()

    def line(self, message: str = "") -> None:
        self.write(f"{message}\n")

    def error(self, message: str) -> None:
        if not self.quiet:
            sys.stderr.write(f"{message}\n")
            sys.stderr.flush()

    def header(self, message: str) -> None:
        self.line(f"=> {message}")

    def detail(self, message: str) -> None:
        self.line(f"   {message}")

    def warn(self, message: str) -> None:
        self.line(f"WARNING: {message}")

    def success(self, message: str) -> None:
        self.line(message)

    @contextmanager
    def progress_bytes(self, description: str, *, total: int) -> Iterator[ProgressCallback]:
        if self.quiet or total <= 0:
            yield lambda _completed: None
            return
        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(binary_units=True),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            console=Console(),
            transient=False,
        )
        with progress:
            task_id = progress.add_task(description, total=total)

            def update(completed: int) -> None:
                progress.update(task_id, completed=max(0, min(completed, total)))

            yield update


def humanize_bytes(value: int) -> str:
    size = float(max(value, 0))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1000 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1000
    return f"{int(size)} B"
