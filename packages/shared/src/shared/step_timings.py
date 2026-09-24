"""Per-step durations for a multi-step operation, logged as one line.

A slow start is found by reading which step took the time, so every step of a
path someone waits on is timed and the line names each one with its seconds.
The same numbers travel in the record's `extra` under `step_seconds` for a log
pipeline that keeps structured fields.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class StepTimings:
    """Durations of named steps, in the order they ran."""

    started: float = field(default_factory=time.monotonic)
    steps: list[tuple[str, float]] = field(default_factory=list)

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """Time the body as `name`, recorded even when it raises."""
        began = time.monotonic()
        try:
            yield
        finally:
            self.steps.append((name, time.monotonic() - began))

    def total_seconds(self) -> float:
        return time.monotonic() - self.started

    def summary(self) -> str:
        parts = [f"total={self.total_seconds():.3f}s"]
        parts.extend(f"{name}={seconds:.3f}s" for name, seconds in self.steps)
        return " ".join(parts)

    def log(
        self,
        logger: logging.Logger,
        message: str,
        *args: object,
        level: int = logging.INFO,
        extra: dict[str, object] | None = None,
    ) -> None:
        """Log `message` followed by every step's duration."""
        fields: dict[str, object] = dict(extra or {})
        fields["step_seconds"] = {name: round(seconds, 3) for name, seconds in self.steps}
        fields["total_seconds"] = round(self.total_seconds(), 3)
        logger.log(level, f"{message}: %s", *args, self.summary(), extra=fields)


__all__ = ["StepTimings"]
