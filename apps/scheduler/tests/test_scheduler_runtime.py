from __future__ import annotations

from identity.token_invalidation import (
    AuthTokenInvalidation,
    configure_token_invalidation,
    configured_token_invalidation,
)
from scheduler.service import Scheduler
from scheduler_app.runtime import SchedulerRuntime
from tests.real_redis import RealRedisActors


def test_scheduler_runtime_close_resets_owned_token_invalidation(
    real_redis_actors: RealRedisActors,
) -> None:
    invalidation = AuthTokenInvalidation.from_redis(real_redis_actors.client())
    configure_token_invalidation(invalidation)
    runtime = SchedulerRuntime(
        scheduler=Scheduler(),
        reset_token_invalidation_on_close=True,
    )

    runtime.close()
    runtime.close()

    assert configured_token_invalidation() is None
