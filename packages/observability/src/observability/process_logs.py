"""Configure application logs on standard error."""

from __future__ import annotations

import logging
import sys
from typing import Protocol, TextIO, runtime_checkable

from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


@runtime_checkable
class _TextStreamHandler(Protocol):
    @property
    def stream(self) -> TextIO: ...


class ProcessLogSettings(BaseSettings):
    # INFO rather than Python's WARNING. A service that says nothing while it
    # works cannot be told from one that is not working.
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_prefix=f"{ENV_PREFIX}_", extra="ignore")

    def resolved_level(self) -> int:
        name = self.log_level.strip().upper()
        if name not in _LEVELS:
            raise ValueError(
                f"{ENV_PREFIX}_LOG_LEVEL is {self.log_level!r}; expected one of "
                + ", ".join(sorted(_LEVELS))
            )
        return getattr(logging, name)


def configure_process_logging(settings: ProcessLogSettings | None = None) -> int:
    """Send this process's own logs to standard error at the configured level.

    Standard error rather than standard output, because a process whose result
    is a document on standard output must not interleave its narration with it.

    Idempotent, and it does not disturb handlers something else installed: a web
    server that has configured its own access logger keeps it, and this adds the
    root handler that application loggers fall back to.
    """

    resolved = (settings or ProcessLogSettings()).resolved_level()
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


__all__ = ["ProcessLogSettings", "configure_process_logging"]
