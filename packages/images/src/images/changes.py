from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from coordination.redis_client import RedisClient, RedisSubscription


@dataclass(slots=True)
class ImageBuildChangeSubscription:
    subscription: RedisSubscription

    def wait(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            message = self.subscription.get_message(
                ignore_subscribe_messages=True, timeout=min(remaining, 1.0)
            )
            if message is not None and message.type in {"message", b"message"}:
                return


@dataclass(slots=True)
class ImageBuildChanges:
    redis: RedisClient

    def publish(self, build_id: str, *, workspace_id: str) -> None:
        self.redis.publish(self._channel(build_id, workspace_id), "1")

    @contextmanager
    def follow(self, build_id: str, *, workspace_id: str) -> Iterator[ImageBuildChangeSubscription]:
        subscription = self.redis.pubsub()
        try:
            subscription.subscribe(self._channel(build_id, workspace_id))
            deadline = time.monotonic() + 5
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("image build notification subscription was not acknowledged")
                message = subscription.get_message(timeout=min(remaining, 1.0))
                if message is not None and message.type in {"subscribe", b"subscribe"}:
                    break
            # Subscribe before the durable read so a commit cannot fall between them.
            yield ImageBuildChangeSubscription(subscription)
        finally:
            subscription.close()

    def _channel(self, build_id: str, workspace_id: str) -> str:
        return self.redis.key("image-build-changes", workspace_id, build_id)
