from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_observer: ContextVar[Callable[[], None] | None] = ContextVar("execution_entry", default=None)


@contextmanager
def observe_execution_entry(observer: Callable[[], None]) -> Iterator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def record_execution_entry() -> None:
    observer = _observer.get()
    if observer is not None:
        observer()
