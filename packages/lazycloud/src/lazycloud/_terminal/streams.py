from __future__ import annotations

import sys
from contextvars import ContextVar
from typing import IO

from rich.console import Console

_json_output_active = ContextVar("lazycloud_cli_json_output_active", default=False)


def set_json_output(enabled: bool) -> None:
    """Reserve stdout for JSON and route decorative output to stderr."""
    _json_output_active.set(enabled)


def json_output_active() -> bool:
    return _json_output_active.get()


class OutputConsole(Console):
    @property
    def file(self) -> IO[str]:
        if self._file is not None:
            stream = self._file
        elif self.stderr or json_output_active():
            stream = sys.stderr
        else:
            stream = sys.stdout
        # Live displays proxy the stream through this console; unwrap it to avoid recursion.
        return getattr(stream, "rich_proxied_file", stream)

    @file.setter
    def file(self, new_file: IO[str]) -> None:
        self._file = new_file


console = OutputConsole()
error_console = OutputConsole(stderr=True)
