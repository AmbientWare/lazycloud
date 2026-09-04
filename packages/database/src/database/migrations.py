from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from database.client import DatabaseClient
from database.settings import DatabaseApplicationName, DatabaseSettings


class DatabaseSchemaState(StrEnum):
    Empty = "empty"
    Current = "current"
    Behind = "behind"
    """Holds a revision this build knows, with later ones still to run."""

    Incompatible = "incompatible"
    """Holds something this build cannot account for, so nothing is attempted."""


@dataclass(frozen=True, slots=True)
class DatabaseSchemaInspection:
    state: DatabaseSchemaState
    current_revision: str | None
    observed_revisions: tuple[str, ...]
    target_revision: str


class DatabaseSchemaMismatchError(RuntimeError):
    """The database holds a revision no migration in this build leads on from.

    Raised rather than guessed at. It means the database was written by a build
    that is not an ancestor of this one, usually a rollback to an image older
    than the schema, and running migrations forward from an unknown point is how
    a partial schema gets written over a working one.
    """

    def __init__(self, inspection: DatabaseSchemaInspection) -> None:
        self.inspection = inspection
        observed = ", ".join(inspection.observed_revisions) or "none"
        super().__init__(
            f"database holds revision {observed}, which is not one this build knows how to "
            f"migrate from; it expects to reach {inspection.target_revision!r}. Deploy a build "
            "whose migrations include that revision, or restore the database."
        )


@dataclass(frozen=True, slots=True)
class DatabaseReadiness:
    revision: str
    attempts: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class DatabaseReadinessProbe:
    expected_revision: str
    observed_revisions: tuple[str, ...]
    attempt: int
    elapsed_seconds: float
    last_error_type: str | None


class DatabaseReadinessTimeoutError(TimeoutError):
    def __init__(
        self,
        *,
        expected_revision: str,
        observed_revisions: tuple[str, ...],
        attempts: int,
        timeout_seconds: float,
        last_error_type: str | None,
    ) -> None:
        self.expected_revision = expected_revision
        self.observed_revisions = observed_revisions
        self.attempts = attempts
        self.timeout_seconds = timeout_seconds
        self.last_error_type = last_error_type
        observed = ", ".join(observed_revisions) if observed_revisions else "none"
        error = f"; last database error: {last_error_type}" if last_error_type else ""
        super().__init__(
            f"database did not reach repository revision {expected_revision!r} within "
            f"{timeout_seconds:g}s after {attempts} attempts; observed revisions: {observed}{error}"
        )


def alembic_config(
    database_url: str | None = None,
    *,
    application_name: DatabaseApplicationName = DatabaseApplicationName.Bootstrap,
) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parent / "alembic"),
    )
    # Left unset when the caller has none: reading the revision graph needs no
    # database, so resolving one here would make listing heads depend on a
    # configured deployment.
    if database_url is not None:
        config.attributes["database_url"] = database_url
    config.attributes["database_application_name"] = application_name.value
    return config


def repository_database_head() -> str:
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one database migration head, found: {heads}")
    return heads[0]


def inspect_database_schema(database_url: str | None = None) -> DatabaseSchemaInspection:
    settings = (
        DatabaseSettings(
            url=database_url,
            application_name=DatabaseApplicationName.Bootstrap,
        )
        if database_url is not None
        else DatabaseSettings(application_name=DatabaseApplicationName.Bootstrap)
    )
    client = DatabaseClient.from_settings(settings)
    try:
        with client.engine.connect() as connection:
            return inspect_database_schema_connection(connection)
    finally:
        client.dispose()


def inspect_database_schema_connection(connection: Connection) -> DatabaseSchemaInspection:
    target_revision = repository_database_head()
    tables = set(inspect(connection).get_table_names())
    application_tables = tables.difference({"alembic_version"})
    observed_revisions = (
        tuple(connection.scalars(text("SELECT version_num FROM alembic_version")))
        if "alembic_version" in tables
        else ()
    )
    current_revision = observed_revisions[0] if len(observed_revisions) == 1 else None

    if not application_tables and not observed_revisions:
        state = DatabaseSchemaState.Empty
    elif observed_revisions == (target_revision,):
        state = DatabaseSchemaState.Current
    elif len(observed_revisions) == 1 and observed_revisions[0] in _known_revisions():
        # An earlier revision this build carries, so there is a path from it.
        state = DatabaseSchemaState.Behind
    else:
        state = DatabaseSchemaState.Incompatible
    return DatabaseSchemaInspection(
        state=state,
        current_revision=current_revision,
        observed_revisions=observed_revisions,
        target_revision=target_revision,
    )


def bootstrap_database(database_url: str | None = None) -> DatabaseSchemaInspection:
    settings = (
        DatabaseSettings(
            url=database_url,
            application_name=DatabaseApplicationName.Bootstrap,
        )
        if database_url is not None
        else DatabaseSettings(application_name=DatabaseApplicationName.Bootstrap)
    )
    client = DatabaseClient.from_settings(settings)
    try:
        with client.engine.connect() as connection:
            return bootstrap_database_connection(connection)
    finally:
        client.dispose()


def bootstrap_database_connection(connection: Connection) -> DatabaseSchemaInspection:
    """Bring the database to the revision this build expects.

    Runs migrations forward from wherever it is, whether that is nothing or an
    earlier revision this build carries. One transaction, so a migration that
    fails part way leaves the database on the revision it started from rather
    than half a schema and a version number that disagrees with it.

    A revision this build has never heard of is refused instead. There is no
    path to compute from an unknown starting point, and inventing one writes
    over a schema somebody else's build is still serving.
    """
    with connection.begin():
        inspection = inspect_database_schema_connection(connection)
        if inspection.state is DatabaseSchemaState.Current:
            return inspection
        if inspection.state is DatabaseSchemaState.Incompatible:
            raise DatabaseSchemaMismatchError(inspection)

        config = alembic_config(str(connection.engine.url))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        completed = inspect_database_schema_connection(connection)
        if completed.state is not DatabaseSchemaState.Current:
            raise RuntimeError(
                "migrations ran but the database did not reach "
                f"{completed.target_revision!r}; it is at {completed.observed_revisions}"
            )
        return completed


def _known_revisions() -> frozenset[str]:
    """Every revision this build can migrate from, which is its whole history."""
    directory = ScriptDirectory.from_config(alembic_config())
    return frozenset(script.revision for script in directory.walk_revisions())


def wait_for_database_head(
    client: DatabaseClient,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    on_poll: Callable[[DatabaseReadinessProbe], None] | None = None,
) -> DatabaseReadiness:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be greater than zero")

    expected_revision = repository_database_head()
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    attempts = 0
    observed_revisions: tuple[str, ...] = ()
    last_error_type: str | None = None
    while True:
        attempts += 1
        try:
            with client.engine.connect() as connection:
                observed_revisions = tuple(
                    connection.scalars(text("SELECT version_num FROM alembic_version"))
                )
            last_error_type = None
        except SQLAlchemyError as exc:
            observed_revisions = ()
            last_error_type = type(exc).__name__

        now = time.monotonic()
        if on_poll is not None:
            on_poll(
                DatabaseReadinessProbe(
                    expected_revision=expected_revision,
                    observed_revisions=observed_revisions,
                    attempt=attempts,
                    elapsed_seconds=now - started_at,
                    last_error_type=last_error_type,
                )
            )
        if observed_revisions == (expected_revision,):
            return DatabaseReadiness(
                revision=expected_revision,
                attempts=attempts,
                elapsed_seconds=now - started_at,
            )
        if now >= deadline:
            raise DatabaseReadinessTimeoutError(
                expected_revision=expected_revision,
                observed_revisions=observed_revisions,
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                last_error_type=last_error_type,
            )
        time.sleep(min(poll_interval_seconds, deadline - now))


__all__ = [
    "DatabaseReadiness",
    "DatabaseReadinessProbe",
    "DatabaseReadinessTimeoutError",
    "DatabaseSchemaInspection",
    "DatabaseSchemaMismatchError",
    "DatabaseSchemaState",
    "alembic_config",
    "bootstrap_database",
    "bootstrap_database_connection",
    "inspect_database_schema",
    "inspect_database_schema_connection",
    "repository_database_head",
    "wait_for_database_head",
]
