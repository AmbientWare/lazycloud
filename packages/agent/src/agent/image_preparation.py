from collections.abc import Callable, Collection
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from dataclasses import dataclass, field
from threading import Event


@dataclass(slots=True)
class WorkerImagePreparation:
    prepare: Callable[[str, Event], None]
    _executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker-image")
    )
    _lookups: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="worker-image-lookup"
        )
    )
    """Apart from preparations, which would otherwise queue behind a lookup's Docker wait."""
    _prepared: set[str] = field(default_factory=set)
    latest: str = ""
    """The image whose preparation finished most recently in this process."""
    _pending: tuple[str, Future[None]] | None = None
    _lookup: tuple[str, Future[None]] | None = None
    _ready: Event = field(default_factory=Event)
    """Set when a lookup finds its image or a preparation ends; cleared once read."""
    _stop: Event = field(default_factory=Event)

    def prepared(self) -> list[str]:
        self._ready.clear()
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

    def look_up(self, image: str, present: Callable[[str, Event], bool]) -> None:
        """Record `image` as prepared, in the background, if `present` finds it on the host.

        `present` gets the stop event `close` sets, so a lookup still waiting
        for Docker ends with the agent.
        """

        def check() -> None:
            if present(image, self._stop):
                self._prepared.add(image)
                self._ready.set()

        self._lookup = (image, self._lookups.submit(check))

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
        if self._pending is not None and self._pending[0] == image and wait_seconds > 0:
            wait_for_futures([self._pending[1]], timeout=wait_seconds)
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
        """Wait for a lookup to find its image or a preparation to end; True when one did."""
        if not self._ready.wait(timeout_seconds):
            return False
        self._ready.clear()
        return True

    def wait_for_started(self, timeout_seconds: float) -> None:
        """Wait for the preparation and lookup in progress to finish, however they end."""
        started = [entry[1] for entry in (self._pending, self._lookup) if entry is not None]
        if started:
            wait_for_futures(started, timeout=timeout_seconds)

    def start(self, image: str) -> Future[None]:
        if self._pending is not None:
            if self._pending[0] != image:
                raise RuntimeError("another worker image is being prepared")
            return self._pending[1]
        operation = self._executor.submit(self.prepare, image, self._stop)
        operation.add_done_callback(lambda _: self._ready.set())
        self._pending = (image, operation)
        return operation

    def close(self) -> None:
        self._stop.set()
        self._lookups.shutdown(wait=True, cancel_futures=True)
        self._executor.shutdown(wait=True, cancel_futures=True)
