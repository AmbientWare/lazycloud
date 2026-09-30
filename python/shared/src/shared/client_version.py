from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from importlib.metadata import version

from packaging.version import InvalidVersion, Version

RECOMMENDED_CLIENT_VERSION_HEADER = "X-Lazycloud-Recommended-Client-Version"

_version_observer: ContextVar[Callable[[str], None] | None] = ContextVar(
    "client_version_observer", default=None
)


@cache
def client_version() -> str:
    # The published client pins shared to its own release version.
    return version("lazycloud-shared")


def release_is_newer(candidate: str, installed: str) -> bool:
    try:
        return Version(candidate) > Version(installed)
    except InvalidVersion:
        return False


@contextmanager
def observe_client_versions(observer: Callable[[str], None]) -> Iterator[None]:
    token = _version_observer.set(observer)
    try:
        yield
    finally:
        _version_observer.reset(token)


def report_client_version(recommended: str | None) -> None:
    observer = _version_observer.get()
    if observer is not None and recommended:
        observer(recommended)
