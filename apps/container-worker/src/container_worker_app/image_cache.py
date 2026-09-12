from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from cache.server import FileCacheServer, WorkerCacheHttpClient, WorkerCacheHttpService
from worker.image_runtime import ImageContentCacheConnection


@dataclass(slots=True)
class ImageContentCacheService:
    cache: FileCacheServer | WorkerCacheHttpClient
    _local_service: WorkerCacheHttpService | None = field(default=None, init=False)

    def start(self) -> ImageContentCacheConnection:
        if isinstance(self.cache, WorkerCacheHttpClient):
            if not self.cache.health().healthy:
                raise RuntimeError("worker image content cache is unhealthy")
            return ImageContentCacheConnection(
                endpoint=self.cache.base_url, token=self.cache.service_token
            )
        service = WorkerCacheHttpService(
            self.cache, service_token=secrets.token_urlsafe(48), port=0
        )
        service.start_in_thread()
        self._local_service = service
        return ImageContentCacheConnection(
            endpoint=service.endpoint.url, token=service.service_token
        )

    def close(self) -> None:
        if self._local_service is not None:
            self._local_service.shutdown()
            self._local_service = None
