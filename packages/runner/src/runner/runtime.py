from __future__ import annotations

import io
from collections.abc import Mapping
from typing import TextIO

DEFAULT_GATEWAY_ENDPOINT = "http://127.0.0.1:9000"
DEFAULT_RUNNER_TIMEOUT_SECONDS = 30.0


class RunnerTaskLogStream(io.TextIOBase):
    def __init__(self, stream: str, wrapped: TextIO) -> None:
        self.stream = stream
        self.wrapped = wrapped
        self._pending = ""
        self.dropped_appends = 0
        self.last_append_error = ""

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        self.wrapped.write(value)
        self.wrapped.flush()
        self._pending += value
        self._flush_complete_lines()
        return len(value)

    def flush(self) -> None:
        self.wrapped.flush()
        self.flush_log()

    def flush_log(self) -> None:
        if self._pending:
            self._append(self._pending)
            self._pending = ""

    def append_log(self, value: str) -> None:
        raise NotImplementedError

    def _flush_complete_lines(self) -> None:
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", maxsplit=1)
            self._append(f"{line}\n")

    def _append(self, value: str) -> None:
        try:
            self.append_log(value)
        except Exception as exc:
            # `write` already put this line on the real stream, so only the
            # platform's copy is lost. Reporting through a logger would write
            # back into this same stream.
            self.dropped_appends += 1
            self.last_append_error = f"{type(exc).__name__}: {exc}"


def required_env(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if not value:
        msg = f"{key} is required"
        raise RuntimeError(msg)
    return value


__all__ = [
    "DEFAULT_GATEWAY_ENDPOINT",
    "DEFAULT_RUNNER_TIMEOUT_SECONDS",
    "RunnerTaskLogStream",
    "required_env",
]
