"""One-time image-build upload capability consumption."""

from __future__ import annotations

from dataclasses import dataclass

from coordination.redis_client import RedisClient

DEFAULT_IMAGE_BUILD_UPLOAD_CAPABILITY_TTL_SECONDS = 5 * 60


@dataclass(slots=True)
class RedisImageBuildUploadCapabilityGuard:
    redis: RedisClient
    ttl_seconds: int = DEFAULT_IMAGE_BUILD_UPLOAD_CAPABILITY_TTL_SECONDS

    def consume(self, capability: str) -> bool:
        if not capability:
            return False
        return self.redis.set(
            self.redis.key("image-build-upload-capabilities", capability),
            "consumed",
            ex=max(self.ttl_seconds, 1),
            nx=True,
        )


__all__ = [
    "DEFAULT_IMAGE_BUILD_UPLOAD_CAPABILITY_TTL_SECONDS",
    "RedisImageBuildUploadCapabilityGuard",
]
