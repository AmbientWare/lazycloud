from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from database.repositories.storage import CacheEntryRepository
from database.tables.storage import CacheEntryTable
from shared.cache_records import CacheEntry
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_cache_entry_upsert_preserves_identity_and_normalizes_sqlite_datetimes(
    tmp_path: Path,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=f"sqlite+pysqlite:///{tmp_path / 'cache.db'}",
            application_name=DatabaseApplicationName.Test,
        )
    )
    database.create_schema()
    created_at = datetime(2026, 7, 19, 12, tzinfo=UTC)
    first = CacheEntry(
        key="build:archive",
        path="/cache/first",
        size=7,
        sha256="a" * 64,
        hits=1,
        expires_at=created_at + timedelta(hours=1),
        created_at=created_at,
        updated_at=created_at,
    )
    replacement = first.model_copy(
        update={
            "path": "/cache/replacement",
            "size": 9,
            "sha256": "b" * 64,
            "hits": 2,
            "created_at": created_at + timedelta(minutes=1),
            "updated_at": created_at + timedelta(minutes=5),
        }
    )

    try:
        with database.session() as session:
            inserted = CacheEntryRepository(session).upsert(first)
            inserted_id = session.scalar(
                select(CacheEntryTable.id).where(CacheEntryTable.key == first.key)
            )
        with database.session() as session:
            updated = CacheEntryRepository(session).upsert(replacement)
            updated_id = session.scalar(
                select(CacheEntryTable.id).where(CacheEntryTable.key == first.key)
            )
            row_count = len(CacheEntryRepository(session).list())
    finally:
        database.dispose()

    assert inserted_id is not None
    assert updated_id == inserted_id
    assert row_count == 1
    assert inserted.created_at == created_at
    assert inserted.created_at.tzinfo is UTC
    assert inserted.expires_at is not None
    assert inserted.expires_at.tzinfo is UTC
    assert updated.created_at == created_at
    assert updated.updated_at == replacement.updated_at
    assert updated.path == replacement.path
    assert updated.size == replacement.size
    assert updated.sha256 == replacement.sha256
    assert updated.hits == replacement.hits


def test_cache_entry_repository_rejects_inverted_relational_timestamps(
    tmp_path: Path,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=f"sqlite+pysqlite:///{tmp_path / 'cache-order.db'}",
            application_name=DatabaseApplicationName.Test,
        )
    )
    database.create_schema()
    created_at = datetime(2026, 7, 19, 13, tzinfo=UTC)
    invalid = CacheEntry(
        key="build:invalid-order",
        path="/cache/invalid-order",
        size=1,
        sha256="c" * 64,
        created_at=created_at,
        updated_at=created_at - timedelta(seconds=1),
    )

    try:
        with pytest.raises(IntegrityError), database.session() as session:
            CacheEntryRepository(session).upsert(invalid)
        with database.session() as session:
            assert CacheEntryRepository(session).get(invalid.key) is None
    finally:
        database.dispose()


def test_cache_entry_repository_increments_hits_in_the_database(
    tmp_path: Path,
) -> None:
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=f"sqlite+pysqlite:///{tmp_path / 'cache-hits.db'}",
            application_name=DatabaseApplicationName.Test,
        )
    )
    database.create_schema()
    created_at = datetime(2026, 7, 19, 14, tzinfo=UTC)
    record = CacheEntry(
        key="build:hits",
        path="/cache/hits",
        size=1,
        sha256="d" * 64,
        created_at=created_at,
        updated_at=created_at,
    )
    incremented_at = created_at + timedelta(minutes=1)

    try:
        with database.session() as session:
            repository = CacheEntryRepository(session)
            repository.upsert(record)
            assert repository.increment_hits("build:missing", updated_at=incremented_at) is None
        with database.session() as session:
            first = CacheEntryRepository(session).increment_hits(
                record.key,
                updated_at=incremented_at,
            )
        with database.session() as session:
            second = CacheEntryRepository(session).increment_hits(
                record.key,
                updated_at=incremented_at + timedelta(seconds=1),
            )
        with database.session() as session:
            third = CacheEntryRepository(session).increment_hits(
                record.key,
                updated_at=created_at - timedelta(days=1),
            )
    finally:
        database.dispose()

    assert first is not None
    assert second is not None
    assert third is not None
    assert first.hits == 1
    assert second.hits == 2
    assert third.hits == 3
    assert third.updated_at == incremented_at + timedelta(seconds=1)
