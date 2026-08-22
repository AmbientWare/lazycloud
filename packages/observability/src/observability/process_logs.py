"""What a long-running process writes about itself.

Python's own default is WARNING to standard error, which for a service means
every deliberate `LOGGER.info` in this repository was discarded and the only
things that reached an operator were unhandled exceptions and whatever the web
server printed about requests. A reconciliation that declined, a pool that came
back unchanged, a step that skipped: all of them wrote a line, and none of them
were read.

`LAZYCLOUD_LOG_LEVEL` already existed and nothing consumed it. It does now.
"""

from __future__ import annotations

import logging
import sys

from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX

_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


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
    if not any(
        isinstance(handler, logging.StreamHandler) and handler.stream is sys.stderr
        for handler in root.handlers
    ):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    return resolved


__all__ = ["ProcessLogSettings", "configure_process_logging"]
