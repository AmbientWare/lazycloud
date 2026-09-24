from collections.abc import Callable, Collection
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event


@dataclass(slots=True)
class WorkerImagePreparation:
    prepare: Callable[[str, Event], None]
    _executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker-image")
    )
    _prepared: set[str] = field(default_factory=set)
    latest: str = ""
    """The image whose preparation finished most recently in this process."""
    _pending: tuple[str, Future[None]] | None = None
    _stop: Event = field(default_factory=Event)

    def prepared(self) -> list[str]:
        pending = self._pending
        if pending is not None and pending[1].done():
            self._pending = None
            pending[1].result()
            self._prepared.add(pending[0])
            self.latest = pending[0]
        return sorted(self._prepared)

    def known(self) -> frozenset[str]:
        """Images already found prepared, without collecting a pending result or its error."""
        return frozenset(self._prepared)

    def mark_prepared(self, image: str) -> None:
        """Record an image found on the host without preparing it again."""
        self._prepared.add(image)

    def ensure(self, image: str, *, wait_seconds: float = 0.0) -> bool:
        """Whether `image` is ready, starting its preparation and waiting briefly if not.

        An image already on the host answers `docker image inspect` in well under
        a second, inside the wait, so a resumed machine reports it on its next
        stream. A pull outlasts the wait and is reported on a later stream.
        """
        if image in self.prepared():
            return True
        if self._pending is None:
            self.start(image)
        if wait_seconds > 0:
            self.wait(wait_seconds)
        return image in self.prepared()

    def unreported(self, reported: Collection[str]) -> bool:
        """Whether an image finished preparing, or failed to, since `reported` was taken.

        Never raises: a failed preparation is raised by `prepared` on the stream
        that reads it, where the daemon handles it.
        """
        pending = self._pending
        if pending is not None and pending[1].done():
            return True
        return bool(self._prepared.difference(reported))

    def wait(self, timeout_seconds: float) -> bool:
        """Wait for the image being prepared; True when one finished, however it ended."""
        pending = self._pending
        if pending is None:
            return False
        try:
            pending[1].exception(timeout=timeout_seconds)
        except TimeoutError:
            return False
        except CancelledError:
            return True
        return True

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
