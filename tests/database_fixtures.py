from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from billing.rate_publication import publish_metered_rate_history
from control.service import ControlPlaneService
from database.context import ServiceContext
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings, bootstrap_database
from tests.backing_services import postgres_url
from tests.fakes import FakeWorkspaceBuckets
from tests.workspaces import owned_workspace


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
def seeded_template_url(postgres_admin: Engine, workspace_template_url: URL) -> Iterator[URL]:
    with temporary_database(postgres_admin, template=workspace_template_url) as url:
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
def workspace_database(
    postgres_admin: Engine, workspace_template_url: URL
) -> Iterator[DatabaseClient]:
    with temporary_database(postgres_admin, template=workspace_template_url) as url:
        client = _client(url)
        try:
            yield client
        finally:
            client.dispose()


@pytest.fixture(scope="session")
def workspace_template_url(
    postgres_admin: Engine, migrated_template_url: URL, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[URL]:
    with temporary_database(postgres_admin, template=migrated_template_url) as url:
        database = DatabaseClient.from_settings(
            DatabaseSettings(
                url=url.render_as_string(hide_password=False),
                application_name=DatabaseApplicationName.Test,
            )
        )
        try:
            context = ServiceContext.create(
                database, root=tmp_path_factory.mktemp("domain"), create_schema=False
            )
            control = ControlPlaneService(context, workspace_storage_client=FakeWorkspaceBuckets())
            owned_workspace(control, "default")
            control.ensure_workspace_storage("default")
        finally:
            database.dispose()
        yield url


@pytest.fixture(scope="session")
def domain_database(postgres_admin: Engine, seeded_template_url: URL) -> Iterator[DatabaseClient]:
    with temporary_database(postgres_admin, template=seeded_template_url) as url:
        database = DatabaseClient.from_settings(
            DatabaseSettings(
                url=url.render_as_string(hide_password=False),
                application_name=DatabaseApplicationName.Test,
            )
        )
        try:
            yield database
        finally:
            database.dispose()


@pytest.fixture
def service_context(domain_database: DatabaseClient, tmp_path: Path) -> Iterator[ServiceContext]:
    # Only single-connection owner tests use this fixture. A commit releases a
    # savepoint; tests of cross-connection visibility keep their real commits.
    with domain_database.engine.connect() as connection, connection.begin() as transaction:
        database = DatabaseClient(
            settings=domain_database.settings,
            engine=domain_database.engine,
            sessions=sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            ),
        )
        try:
            yield ServiceContext.create(database, root=tmp_path, create_schema=False)
        finally:
            assert transaction.is_active, "owner test ended its outer isolation transaction"
            transaction.rollback()


@pytest.fixture
def committed_service_context(seeded_database: DatabaseClient, tmp_path: Path) -> ServiceContext:
    """Owner services whose transactions must commit across independent connections."""
    return ServiceContext.create(seeded_database, root=tmp_path, create_schema=False)


@pytest.fixture
def seeded_database_url(postgres_admin: Engine, seeded_template_url: URL) -> Iterator[URL]:
    with temporary_database(postgres_admin, template=seeded_template_url) as url:
        yield url


@pytest.fixture
def seeded_database(seeded_database_url: URL) -> Iterator[DatabaseClient]:
    client = _client(seeded_database_url)
    try:
        yield client
    finally:
        client.dispose()
