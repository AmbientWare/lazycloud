"""Configure application logs on standard error."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from typing import Protocol, TextIO, runtime_checkable

from shared.app_identity import ENV_PREFIX

LOG_LEVEL_ENV = f"{ENV_PREFIX}_LOG_LEVEL"
_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


@runtime_checkable
class _TextStreamHandler(Protocol):
    @property
    def stream(self) -> TextIO: ...


def process_log_level(environ: Mapping[str, str] | None = None) -> int:
    """The level named by the log level variable, INFO when it is unset.

    INFO rather than Python's WARNING. A service that says nothing while it
    works cannot be told from one that is not working.
    """
    value = (os.environ if environ is None else environ).get(LOG_LEVEL_ENV, "INFO")
    name = value.strip().upper()
    if name not in _LEVELS:
        raise ValueError(
            f"{LOG_LEVEL_ENV} is {value!r}; expected one of " + ", ".join(sorted(_LEVELS))
        )
    return getattr(logging, name)


def configure_process_logging(level: int | None = None) -> int:
    """Send this process's own logs to standard error at the configured level.

    Standard error rather than standard output, because a process whose result
    is a document on standard output must not interleave its narration with it.

    Idempotent, and it does not disturb handlers something else installed: a web
    server that has configured its own access logger keeps it, and this adds the
    root handler that application loggers fall back to.
    """

    resolved = process_log_level() if level is None else level
    root = logging.getLogger()
    root.setLevel(resolved)
    # HTTP transport logs include credential-bearing URLs and headers. Provider
    # adapters report failures without copying those requests into the log.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if not any(
        isinstance(handler, _TextStreamHandler) and handler.stream is sys.stderr
        for handler in root.handlers
    ):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    return resolved


__all__ = ["LOG_LEVEL_ENV", "configure_process_logging", "process_log_level"]
