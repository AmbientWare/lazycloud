from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from billing.rate_publication import publish_metered_rate_history
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings, bootstrap_database
from tests.backing_services import postgres_url


@pytest.fixture(scope="session")
def postgres_admin() -> Iterator[Engine]:
    admin = create_engine(
        postgres_url(),
        isolation_level="AUTOCOMMIT",
        connect_args={"application_name": DatabaseApplicationName.Test.value},
    )
    try:
        yield admin
    finally:
        admin.dispose()


@contextmanager
def temporary_database(admin: Engine, *, template: URL | None = None) -> Iterator[URL]:
    name = f"lazycloud_test_{uuid4().hex}"
    quote = admin.dialect.identifier_preparer.quote_identifier
    source = f" TEMPLATE {quote(template.database)}" if template and template.database else ""
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {quote(name)}{source}"))
    try:
        yield admin.url.set(database=name)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE {quote(name)} WITH (FORCE)"))
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM pg_database WHERE datname = :name"), {"name": name}
                )
                == 0
            )


@pytest.fixture
def postgres_database_url(postgres_admin: Engine) -> Iterator[URL]:
    """An empty database for tests that exercise schema creation and migration."""
    with temporary_database(postgres_admin) as url:
        yield url


@pytest.fixture(scope="session")
def migrated_template_url(postgres_admin: Engine) -> Iterator[URL]:
    with temporary_database(postgres_admin) as url:
        bootstrap_database(url.render_as_string(hide_password=False))
        yield url


@pytest.fixture(scope="session")
def seeded_template_url(postgres_admin: Engine, migrated_template_url: URL) -> Iterator[URL]:
    with temporary_database(postgres_admin, template=migrated_template_url) as url:
        database = _client(url)
        try:
            with database.session() as session:
                publish_metered_rate_history(session)
        finally:
            database.dispose()
        yield url


def _client(url: URL) -> DatabaseClient:
    dsn = url.render_as_string(hide_password=False)
    return DatabaseClient.from_settings(
        DatabaseSettings(url=dsn, direct_url=dsn, application_name=DatabaseApplicationName.Test)
    )


@pytest.fixture
def migrated_database_url(postgres_admin: Engine, migrated_template_url: URL) -> Iterator[URL]:
    with temporary_database(postgres_admin, template=migrated_template_url) as url:
        yield url


@pytest.fixture
def database(migrated_database_url: URL) -> Iterator[DatabaseClient]:
    client = _client(migrated_database_url)
    try:
        yield client
    finally:
        client.dispose()


@pytest.fixture
def seeded_database(postgres_admin: Engine, seeded_template_url: URL) -> Iterator[DatabaseClient]:
    # Only immutable rates are shared. Accounts, workspaces, sequences and writes
    # belong to each clone, including commits made by another connection or thread.
    with temporary_database(postgres_admin, template=seeded_template_url) as url:
        client = _client(url)
        try:
            yield client
        finally:
            client.dispose()
