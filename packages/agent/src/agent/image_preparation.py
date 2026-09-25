from collections.abc import Callable, Collection
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
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
    _lookup: Future[None] | None = None
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

    def look_up(self, image: str, present: Callable[[str, Event], bool]) -> None:
        """Record `image` as prepared, in the background, if `present` finds it on the host.

        `present` gets the stop event `close` sets, so a lookup still waiting
        for Docker ends with the agent.
        """

        def check() -> None:
            if present(image, self._stop):
                self._prepared.add(image)

        self._lookup = self._executor.submit(check)

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
        """Wait for an image being prepared or looked up; True when one finished."""
        waiting: list[Future[None]] = []
        if self._pending is not None:
            waiting.append(self._pending[1])
        if self._lookup is not None:
            waiting.append(self._lookup)
        if not waiting:
            return False
        done, _ = wait_for_futures(waiting, timeout=timeout_seconds, return_when=FIRST_COMPLETED)
        if self._lookup is not None and self._lookup in done:
            self._lookup = None
        return bool(done)

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
