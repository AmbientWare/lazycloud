from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from math import ceil

from coordination.redis_client import RedisClient
from shared.step_timings import StepTimings

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FleetPass:
    redis: RedisClient
    timings: StepTimings = field(default_factory=StepTimings)
    failures: list[str] = field(default_factory=list)

    def run[T](self, name: str, action: Callable[[], T], default: T, *, interval: float = 0) -> T:
        with self.timings.step(name):
            try:
                # Keep the cadence claim after completion so another replica
                # cannot repeat the same reads immediately after this pass.
                if interval > 0 and not self.redis.set(
                    self.redis.key("fleet", "cadence", name), "1", nx=True, ex=ceil(interval)
                ):
                    return default
                return action()
            except Exception:
                self.failures.append(name)
                LOGGER.exception("fleet %s failed", name)
                return default

    def finish(self) -> None:
        if self.failures:
            raise RuntimeError(f"fleet pass failed: {', '.join(self.failures)}")
        if self.timings.total_seconds() >= 2:
            self.timings.log(LOGGER, "fleet pass was slow")
