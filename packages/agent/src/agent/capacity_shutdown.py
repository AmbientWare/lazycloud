from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, Lock, Timer
from typing import Protocol

from shared.timestamps import utc_now

DEFAULT_INTERRUPTION_GRACE_SECONDS = 15.0
INTERRUPTION_SAFETY_SECONDS = 5.0

LOGGER = logging.getLogger(__name__)


class CapacityWorkers(Protocol):
    def gracefully_stop_all(self, *, grace_seconds: float) -> None: ...

    def stop_all(self) -> None: ...


@dataclass(slots=True)
class CapacityShutdown:
    workers: CapacityWorkers
    grace_seconds: float = DEFAULT_INTERRUPTION_GRACE_SECONDS
    deadline: datetime | None = field(default=None, init=False)
    started: Event = field(default_factory=Event, init=False)
    _timer: Timer | None = field(default=None, init=False)
    _lock: Lock = field(default_factory=Lock, init=False)
    _stopped: bool = field(default=False, init=False)

    def arm(self, deadline: datetime) -> None:
        if self.deadline is not None and self.deadline <= deadline:
            return
        self.deadline = deadline
        if self._timer is not None:
            self._timer.cancel()
        delay = max(
            0.0,
            (deadline - utc_now()).total_seconds()
            - self.grace_seconds
            - INTERRUPTION_SAFETY_SECONDS,
        )
        # Gateway calls may block through the notice window. Shutdown cannot depend on them.
        self._timer = Timer(delay, self._stop_on_deadline)
        self._timer.daemon = True
        self._timer.start()

    def due(self) -> bool:
        return self.started.is_set() or (
            self.deadline is not None
            and (self.deadline - utc_now()).total_seconds()
            <= self.grace_seconds + INTERRUPTION_SAFETY_SECONDS
        )

    def stop(self, *, force: bool = False) -> None:
        with self._lock:
            if self._stopped:
                return
            self.started.set()
            grace = self.grace_seconds
            if self.deadline is not None:
                grace = min(
                    grace,
                    (self.deadline - utc_now()).total_seconds() - INTERRUPTION_SAFETY_SECONDS,
                )
            if grace > 0 and not force:
                try:
                    self.workers.gracefully_stop_all(grace_seconds=grace)
                except Exception:
                    LOGGER.exception(
                        "graceful interruption shutdown failed; forcing worker removal"
                    )
                    self.workers.stop_all()
            else:
                self.workers.stop_all()
            self._stopped = True

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def _stop_on_deadline(self) -> None:
        try:
            self.stop()
        except Exception:
            LOGGER.exception("provider interruption worker shutdown failed")
