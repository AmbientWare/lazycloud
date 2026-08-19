"""Where a test finds the real Redis and PostgreSQL it needs, and what it says
when they are not there.

One owner for both, because the answer to "did this run prove what it looks
like it proved" is the same question for either, and it was previously answered
in seven places under three environment variable names — two of which pointed
at the same PostgreSQL instance. A run that set one and not the other read as
green while the proofs it skipped were the ones a fake backend cannot stand in
for: single-winner races, `FOR UPDATE` semantics, expired-lease recovery.

Skipping stays a skip rather than a failure, so `pytest` still works on a
machine with nothing running. What changes is that it is audible: the session
reports at the end what it did not prove, instead of printing a dot for it.
"""

from __future__ import annotations

import os
from typing import NoReturn

import pytest
from sqlalchemy.engine import URL, make_url

REDIS_URL_VARIABLE = "LAZYCLOUD_TEST_REDIS_URL"
POSTGRES_URL_VARIABLE = "LAZYCLOUD_TEST_POSTGRES_URL"

_unproven: set[str] = set()


def redis_url() -> str:
    """The real Redis to run against, or skip loudly."""

    url = os.environ.get(REDIS_URL_VARIABLE)
    if not url:
        _skip(REDIS_URL_VARIABLE, "real Redis")
    return url


def postgres_url() -> URL:
    """The real PostgreSQL to run against, or skip loudly.

    The caller gets a base URL to connect with, not a database to use: the
    fixtures here create a throwaway database per test off it and drop it after,
    so the one this names is only ever connected to in order to issue `CREATE
    DATABASE`. Pointing it at a database holding anything is therefore safe, and
    pointing it at one that does not exist is not.
    """

    value = os.environ.get(POSTGRES_URL_VARIABLE)
    if not value:
        _skip(POSTGRES_URL_VARIABLE, "real PostgreSQL")
    url = make_url(value)
    if url.get_backend_name() != "postgresql":
        _skip(POSTGRES_URL_VARIABLE, f"real PostgreSQL, but it names {url.get_backend_name()}")
    return url


def postgres_dsn() -> str:
    """The same URL rendered for a caller that wants a string.

    Its own function so that `hide_password=False` is decided once. Rendered the
    default way the password becomes `***` and the connection fails somewhere far
    from here, and three call sites each remembering the keyword is three chances
    to forget it.
    """

    return postgres_url().render_as_string(hide_password=False)


def unproven_services() -> list[str]:
    """What this session skipped for want of a service, for the summary line."""

    return sorted(_unproven)


def _skip(variable: str, needs: str) -> NoReturn:
    _unproven.add(variable)
    pytest.skip(f"{variable} is unset: this proof needs {needs}")
