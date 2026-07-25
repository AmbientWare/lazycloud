from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from tests.service_fixtures import isolated_services

from database import DatabaseApplicationName


@pytest.fixture
def postgres_database_url() -> Iterator[URL]:
    value = os.getenv("LAZYCLOUD_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("LAZYCLOUD_TEST_DATABASE_URL is not configured")
    base_url = make_url(value)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("LAZYCLOUD_TEST_DATABASE_URL is not PostgreSQL")

    database_name = f"lazycloud_test_{uuid4().hex}"
    database_url = base_url.set(database=database_name)
    admin = create_engine(
        base_url,
        connect_args={"application_name": DatabaseApplicationName.Test.value},
    )
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            identifier = connection.dialect.identifier_preparer.quote_identifier(database_name)
            connection.execute(text(f"CREATE DATABASE {identifier}"))
        yield database_url
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            identifier = connection.dialect.identifier_preparer.quote_identifier(database_name)
            connection.execute(text(f"DROP DATABASE IF EXISTS {identifier} WITH (FORCE)"))
            remaining = connection.scalar(
                text("SELECT count(*) FROM pg_database WHERE datname = :database_name"),
                {"database_name": database_name},
            )
            assert remaining == 0
        admin.dispose()


__all__ = ["isolated_services", "postgres_database_url"]
