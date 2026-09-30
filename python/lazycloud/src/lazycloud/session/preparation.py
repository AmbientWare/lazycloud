from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from threading import Lock
from types import TracebackType

from shared.image_building.authoring import ImageSpec
from typing_extensions import Self

MAX_DEPLOYMENT_PREPARATIONS = 4


class DeploymentPreparation:
    """Share preparation work for the lifetime of one deployment operation."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_DEPLOYMENT_PREPARATIONS,
            thread_name_prefix="deployment-image",
        )
        self._lock = Lock()
        self._images: dict[str, Future[ImageSpec]] = {}
        self._sources: dict[str, Future[str]] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._images.clear()
        self._sources.clear()

    def image(self, key: str, build: Callable[[], ImageSpec]) -> Future[ImageSpec]:
        with self._lock:
            existing = self._images.get(key)
            if existing is not None:
                return existing
            future = self._executor.submit(copy_context().run, build)
            self._images[key] = future
            return future

    def source(self, key: str, upload: Callable[[], str]) -> str:
        future: Future[str] = Future()
        with self._lock:
            existing = self._sources.get(key)
            if existing is None:
                self._sources[key] = future
        if existing is not None:
            return existing.result()
        try:
            result = upload()
        except BaseException as exc:
            future.set_exception(exc)
            raise
        future.set_result(result)
        return result
