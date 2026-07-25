from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from database.tables import DatabaseBase
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SAWarning

from database import (
    DatabaseSchemaMismatchError,
    DatabaseSchemaState,
    bootstrap_database,
    inspect_database_schema,
    repository_database_head,
)


def test_empty_database_bootstraps_once_through_the_current_baseline(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'fresh.db'}"

    before = inspect_database_schema(database_url)
    first = bootstrap_database(database_url)
    repeated = bootstrap_database(database_url)

    assert before.state is DatabaseSchemaState.Empty
    assert first == repeated
    assert first.state is DatabaseSchemaState.Current
    assert first.current_revision == repository_database_head()
    assert first.observed_revisions == (repository_database_head(),)


@pytest.mark.parametrize("shape", ["application-without-revision", "revision-without-schema"])
def test_nonempty_noncurrent_database_fails_closed_without_mutation(
    tmp_path: Path,
    shape: str,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / f'{shape}.db'}"
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            if shape == "application-without-revision":
                connection.execute(text("CREATE TABLE retained_marker (value VARCHAR NOT NULL)"))
                connection.execute(text("INSERT INTO retained_marker VALUES ('preserved')"))
            else:
                connection.execute(
                    text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
                )
                connection.execute(text("INSERT INTO alembic_version VALUES ('obsolete_revision')"))

        with pytest.raises(
            DatabaseSchemaMismatchError,
            match="only after confirming it is local and disposable",
        ) as raised:
            bootstrap_database(database_url)

        assert raised.value.inspection.state is DatabaseSchemaState.Incompatible
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            if shape == "application-without-revision":
                assert connection.scalar(text("SELECT value FROM retained_marker")) == "preserved"
                assert tables == {"retained_marker"}
            else:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                    "obsolete_revision"
                )
                assert tables == {"alembic_version"}
    finally:
        engine.dispose()


def test_stale_full_schema_is_never_upgraded_or_reset(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'stale.db'}"
    bootstrap_database(database_url)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = 'obsolete_revision'"))
            tables_before = set(inspect(connection).get_table_names())

        with pytest.raises(DatabaseSchemaMismatchError) as raised:
            bootstrap_database(database_url)

        assert raised.value.inspection.observed_revisions == ("obsolete_revision",)
        with engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) == tables_before
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "obsolete_revision"
            )
    finally:
        engine.dispose()


def test_postgresql_baseline_matches_metadata_constraints_and_indexes(
    postgres_database_url: URL,
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    result = bootstrap_database(database_url)
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            actual_tables = set(inspector.get_table_names()).difference({"alembic_version"})
            with pytest.warns(SAWarning, match="Cannot correctly sort tables"):
                differences = compare_metadata(
                    MigrationContext.configure(
                        connection,
                        opts={"compare_type": True, "compare_server_default": True},
                    ),
                    DatabaseBase.metadata,
                )
            extension_installed = connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto')")
            )
            actual_indexes = {
                str(item["name"])
                for table_name in actual_tables
                for item in inspector.get_indexes(table_name)
                if item.get("name") is not None and item.get("duplicates_constraint") is None
            }
            actual_checks = {
                str(item["name"])
                for table_name in actual_tables
                for item in inspector.get_check_constraints(table_name)
                if item.get("name") is not None
            }
            actual_uniques = {
                str(item["name"])
                for table_name in actual_tables
                for item in inspector.get_unique_constraints(table_name)
                if item.get("name") is not None
            }
            actual_foreign_keys = {
                (
                    table_name,
                    tuple(str(column) for column in item["constrained_columns"]),
                    str(item["referred_table"]),
                    tuple(str(column) for column in item["referred_columns"]),
                    item.get("options", {}).get("ondelete"),
                )
                for table_name in actual_tables
                for item in inspector.get_foreign_keys(table_name)
            }
    finally:
        engine.dispose()

    expected_indexes = {
        index.name
        for table in DatabaseBase.metadata.tables.values()
        for index in table.indexes
        if index.name is not None
    }
    expected_checks = {
        constraint.name
        for table in DatabaseBase.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }
    expected_uniques = {
        constraint.name
        for table in DatabaseBase.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint) and constraint.name is not None
    }
    expected_foreign_keys = {
        (
            table.name,
            tuple(element.parent.name for element in constraint.elements),
            constraint.referred_table.name,
            tuple(element.column.name for element in constraint.elements),
            constraint.ondelete,
        )
        for table in DatabaseBase.metadata.tables.values()
        for constraint in table.foreign_key_constraints
    }

    assert result.state is DatabaseSchemaState.Current
    assert actual_tables == set(DatabaseBase.metadata.tables)
    assert differences == []
    assert extension_installed is True
    assert actual_indexes == expected_indexes
    assert actual_checks == expected_checks
    assert actual_uniques == expected_uniques
    assert actual_foreign_keys == expected_foreign_keys
