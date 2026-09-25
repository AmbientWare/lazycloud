"""Noticing that the machine slept, as a hibernated reserve does before it resumes.

CLOCK_MONOTONIC stops while the machine sleeps and CLOCK_BOOTTIME does not, so
the gap between them grows by the length of each sleep.
"""

from __future__ import annotations

import ctypes
import logging
import os
import select
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from threading import Lock, Thread

from shared.step_timings import seconds_since_boot

LOGGER = logging.getLogger(__name__)

SLEEP_THRESHOLD_SECONDS = 1.0

_CLOCK_REALTIME = 0
_TFD_NONBLOCK = 0o4000
_TFD_CLOEXEC = 0o2000000
_TFD_TIMER_ABSTIME = 1
_TFD_TIMER_CANCEL_ON_SET = 2
_FAR_FUTURE_SECONDS = 10 * 365 * 24 * 3600


def _sleep_offset() -> float:
    return seconds_since_boot() - time.monotonic()


@dataclass(slots=True)
class SuspendWatch:
    """Reports each sleep once, with how long it lasted."""

    _offset: float = field(default_factory=_sleep_offset, init=False)

    def slept(self) -> float:
        """Seconds slept since the last call, or 0 when the machine stayed awake."""
        offset = _sleep_offset()
        slept, self._offset = offset - self._offset, offset
        return slept if slept > SLEEP_THRESHOLD_SECONDS else 0.0


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class _Itimerspec(ctypes.Structure):
    _fields_ = [("it_interval", _Timespec), ("it_value", _Timespec)]


class _ClockJumps:
    """A Linux timerfd the kernel cancels whenever the wall clock jumps.

    The kernel treats a resume from sleep as a clock change, so the descriptor
    turns readable the moment a hibernated machine runs again.
    """

    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        descriptor = self._libc.timerfd_create(_CLOCK_REALTIME, _TFD_NONBLOCK | _TFD_CLOEXEC)
        if descriptor < 0:
            raise OSError(ctypes.get_errno(), "timerfd_create failed")
        self.descriptor: int = descriptor
        try:
            self.arm()
        except OSError:
            os.close(descriptor)
            raise

    def arm(self) -> None:
        spec = _Itimerspec()
        spec.it_value.tv_sec = int(time.time()) + _FAR_FUTURE_SECONDS
        flags = _TFD_TIMER_ABSTIME | _TFD_TIMER_CANCEL_ON_SET
        if self._libc.timerfd_settime(self.descriptor, flags, ctypes.byref(spec), None) < 0:
            raise OSError(ctypes.get_errno(), "timerfd_settime failed")

    def acknowledge(self) -> None:
        """Consume the cancellation and watch for the next jump."""
        with suppress(OSError):
            os.read(self.descriptor, 8)
        self.arm()

    def close(self) -> None:
        os.close(self.descriptor)


class Wakeup:
    """A wait that ends at its timeout, on `wake()`, or when the machine resumes from sleep.

    Only the thread that owns the waiter waits; any thread may call `wake()`.
    A watch thread blocks on the kernel's clock jump notice. When a jump comes
    with a real sleep, not a clock step, it calls `on_resume`, which reaches
    work the owner is itself blocked in, then wakes the waiter. Without that
    notice a resume would go unseen, so a kernel that cannot give one fails the
    agent at start, and a watch that fails later fails the next wait.
    """

    def __init__(self, on_resume: Callable[[], None] | None = None) -> None:
        try:
            self._jumps: _ClockJumps | None = _ClockJumps()
        except (OSError, AttributeError) as exc:
            msg = "the agent needs a Linux timerfd clock jump notice to see a resume from sleep"
            raise RuntimeError(msg) from exc
        self._read, self._write = os.pipe()
        self._stop_read, self._stop_write = os.pipe()
        for descriptor in (self._read, self._write, self._stop_read, self._stop_write):
            os.set_blocking(descriptor, False)
        self._on_resume = on_resume
        self._suspend = SuspendWatch()
        self._lock = Lock()
        self._closed = False
        self._failure: OSError | None = None
        self._watch = Thread(
            target=self._watch_jumps, args=(self._jumps,), name="clock-jumps", daemon=True
        )
        self._watch.start()

    def wake(self) -> None:
        with self._lock:
            if self._closed:
                return
            with suppress(BlockingIOError, OSError):
                os.write(self._write, b"\0")

    def wait(self, timeout_seconds: float) -> None:
        if self._failure is not None:
            msg = "the clock jump watch failed, so a resume from sleep would go unseen"
            raise RuntimeError(msg) from self._failure
        ready, _, _ = select.select([self._read], [], [], max(timeout_seconds, 0.0))
        if ready:
            while True:
                try:
                    if not os.read(self._read, 64):
                        break
                except BlockingIOError:
                    break

    def _watch_jumps(self, jumps: _ClockJumps) -> None:
        while True:
            try:
                ready, _, _ = select.select([jumps.descriptor, self._stop_read], [], [])
                if self._stop_read in ready:
                    return
                jumps.acknowledge()
            except OSError as exc:
                self._jumps = None
                with suppress(OSError):
                    jumps.close()
                if not self._closed:
                    LOGGER.error("the clock jump watch failed", exc_info=True)
                    self._failure = exc
                    self.wake()
                return
            if self._suspend.slept() and self._on_resume is not None:
                try:
                    self._on_resume()
                except Exception:
                    LOGGER.warning("acting on a resume failed", exc_info=True)
            self.wake()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        with suppress(BlockingIOError, OSError):
            os.write(self._stop_write, b"\0")
        self._watch.join(timeout=1.0)
        if self._jumps is not None:
            self._jumps.close()
        for descriptor in (self._read, self._write, self._stop_read, self._stop_write):
            os.close(descriptor)
