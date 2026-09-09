from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from database.repositories.storage import CacheEntryRepository
from database.tables.storage import CacheEntryTable
from shared.cache_records import CacheEntry
from sqlalchemy import select
from sqlalchemy.engine import URL

from database import (
    DatabaseApplicationName,
    DatabaseClient,
    DatabaseSettings,
)


def test_postgresql_concurrent_cache_upsert_preserves_one_canonical_row(
    migrated_database_url: URL,
) -> None:
    database_url = migrated_database_url.render_as_string(hide_password=False)
    database = _client(database_url, pool_size=2)
    created_at = datetime(2026, 7, 19, 13, tzinfo=UTC)
    candidates = (
        CacheEntry(
            key="build:concurrent",
            path="/cache/first",
            size=1,
            sha256="a" * 64,
            hits=1,
            created_at=created_at,
            updated_at=created_at,
        ),
        CacheEntry(
            key="build:concurrent",
            path="/cache/second",
            size=2,
            sha256="b" * 64,
            hits=2,
            created_at=created_at + timedelta(seconds=1),
            updated_at=created_at + timedelta(minutes=1),
        ),
    )
    barrier = Barrier(2)

    def write(record: CacheEntry) -> CacheEntry:
        barrier.wait()
        with database.session() as session:
            return CacheEntryRepository(session).upsert(record)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(write, candidates))
        with database.session() as session:
            rows = CacheEntryRepository(session).list()
            row_ids = tuple(session.scalars(select(CacheEntryTable.id)))
    finally:
        database.dispose()

    assert len(results) == 2
    assert len(rows) == len(row_ids) == 1
    assert rows[0].updated_at == max(candidate.updated_at for candidate in candidates)


def test_postgresql_cache_hit_increment_is_atomic(
    migrated_database_url: URL,
) -> None:
    database_url = migrated_database_url.render_as_string(hide_password=False)
    database = _client(database_url, pool_size=8)
    created_at = datetime(2026, 7, 19, 14, tzinfo=UTC)
    record = CacheEntry(
        key="build:concurrent-hits",
        path="/cache/concurrent-hits",
        size=1,
        sha256="c" * 64,
        created_at=created_at,
        updated_at=created_at,
    )
    incremented_at = created_at + timedelta(minutes=1)
    barrier = Barrier(8)

    def increment(_index: int) -> CacheEntry:
        barrier.wait()
        with database.session() as session:
            incremented = CacheEntryRepository(session).increment_hits(
                record.key,
                updated_at=incremented_at,
            )
        assert incremented is not None
        return incremented

    try:
        with database.session() as session:
            CacheEntryRepository(session).upsert(record)
        with ThreadPoolExecutor(max_workers=8) as executor:
            increments = tuple(executor.map(increment, range(8)))
        with database.session() as session:
            current = CacheEntryRepository(session).get(record.key)
    finally:
        database.dispose()

    assert current is not None
    assert sorted(item.hits for item in increments) == list(range(1, 9))
    assert current.hits == 8
    assert current.updated_at == incremented_at


def _client(database_url: str, *, pool_size: int) -> DatabaseClient:
    return DatabaseClient.from_settings(
        DatabaseSettings(
            url=database_url,
            pool_size=pool_size,
            max_overflow=0,
            statement_timeout_ms=0,
            application_name=DatabaseApplicationName.Test,
        )
    )
