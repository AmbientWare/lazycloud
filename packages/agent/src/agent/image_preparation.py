from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event


@dataclass(slots=True)
class WorkerImagePreparation:
    prepare: Callable[[str, Event], None]
    _executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker-image")
    )
    _prepared: set[str] = field(default_factory=set)
    _pending: tuple[str, Future[None]] | None = None
    _stop: Event = field(default_factory=Event)

    def prepared(self) -> list[str]:
        pending = self._pending
        if pending is not None and pending[1].done():
            self._pending = None
            pending[1].result()
            self._prepared.add(pending[0])
        return sorted(self._prepared)

    def ensure(self, image: str) -> bool:
        if image in self.prepared():
            return True
        if self._pending is None:
            self.start(image)
        return False

    def start(self, image: str) -> Future[None]:
        if self._pending is not None:
            if self._pending[0] != image:
                raise RuntimeError("another worker image is being prepared")
            return self._pending[1]
        operation = self._executor.submit(self.prepare, image, self._stop)
        self._pending = (image, operation)
        return operation

    def close(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
