"""Recording a reserve's finished stop within seconds of the provider finishing it."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock, Thread

LOGGER = logging.getLogger(__name__)

STOP_RECHECK_SECONDS = 5.0
STOP_RECHECK_LIMIT_SECONDS = 600.0
"""How long a unit is rechecked; a stop still pending then is the capacity pass's to finish."""


@dataclass(slots=True)
class StopRechecks:
    """Rechecks a unit every few seconds while a stop its provider accepted finishes.

    A capacity pass records a finished stop only on its next run, and an
    acquisition before then finds no stopped reserve to resume and buys one. A
    single thread serves every unit with a stop under way and ends once none is
    left, so nothing is read while no stop is pending.
    """

    recheck: Callable[[str, str], bool]
    """Describes and records one unit; true while a stop it accepted is still under way."""
    interval_seconds: float = STOP_RECHECK_SECONDS
    limit_seconds: float = STOP_RECHECK_LIMIT_SECONDS
    _pending: dict[tuple[str, str], float] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)
    _running: bool = False

    def watch(self, workspace_id: str, capacity_owner_id: str) -> None:
        with self._lock:
            self._pending.setdefault(
                (workspace_id, capacity_owner_id), time.monotonic() + self.limit_seconds
            )
            if self._running:
                return
            self._running = True
        Thread(target=self._run, name="stop-rechecks", daemon=True).start()

    def _run(self) -> None:
        while True:
            time.sleep(self.interval_seconds)
            with self._lock:
                due = list(self._pending.items())
            for key, deadline in due:
                try:
                    pending = self.recheck(*key)
                except Exception:
                    LOGGER.warning("rechecking the stop in %s failed", key[1], exc_info=True)
                    pending = True
                if not pending or time.monotonic() >= deadline:
                    with self._lock:
                        self._pending.pop(key, None)
            with self._lock:
                if not self._pending:
                    self._running = False
                    return


__all__ = ["STOP_RECHECK_LIMIT_SECONDS", "STOP_RECHECK_SECONDS", "StopRechecks"]
