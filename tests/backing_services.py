"""Test-only Compose endpoints, overridden explicitly when reusing services or in CI."""

from __future__ import annotations

import os

from sqlalchemy.engine import URL, make_url

REDIS_URL_VARIABLE = "LAZYCLOUD_TEST_REDIS_URL"
POSTGRES_URL_VARIABLE = "LAZYCLOUD_TEST_POSTGRES_URL"


def redis_url() -> str:
    return os.environ.get(REDIS_URL_VARIABLE, "redis://127.0.0.1:16379/0")


def postgres_url() -> URL:
    url = make_url(
        os.environ.get(
            POSTGRES_URL_VARIABLE,
            "postgresql+psycopg://lazycloud:lazycloud@127.0.0.1:15432/postgres",
        )
    )
    if url.get_backend_name() != "postgresql":
        raise ValueError(f"{POSTGRES_URL_VARIABLE} must name a PostgreSQL server")
    return url


def postgres_dsn() -> str:
    return postgres_url().render_as_string(hide_password=False)
