from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from api.server.services import ApiServices
from database.repositories.storage import CacheEntryRepository
from shared.cache_records import CacheEntry
from storage.service import CacheStorage, MountedCacheClient, MountedCacheSettings


class _FailOnceDeleteCacheClient(MountedCacheClient):
    def __init__(self, settings: MountedCacheSettings) -> None:
        super().__init__(settings)
        self.fail_path: Path | None = None

    def delete_path(self, path: str | Path) -> None:
        if self.fail_path == Path(path):
            self.fail_path = None
            raise OSError("injected cache byte deletion failure")
        super().delete_path(path)


def _cache_storage(
    isolated_services: ApiServices,
    tmp_path: Path,
    *,
    client: MountedCacheClient | None = None,
) -> tuple[CacheStorage, MountedCacheClient]:
    cache_client = client or MountedCacheClient(
        MountedCacheSettings(root=tmp_path / "mounted-cache")
    )
    return (
        CacheStorage(isolated_services.context, cache_client=cache_client),
        cache_client,
    )


def test_mounted_cache_materializations_are_immutable(tmp_path: Path) -> None:
    client = MountedCacheClient(MountedCacheSettings(root=tmp_path / "cache"))
    logical_key = "a" * 64
    object_key = f"{logical_key}{hashlib.sha256(b'first').hexdigest()}"

    first = client.put_bytes(object_key, b"first")
    repeated = client.put_bytes(object_key, b"first")

    assert first.created is True
    assert repeated.created is False
    assert repeated.path == first.path
    assert first.path.read_bytes() == b"first"
    with pytest.raises(ValueError, match="does not match the materialized content"):
        client.put_bytes(object_key, b"second")
    first.path.write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="conflicts with immutable content"):
        client.put_bytes(object_key, b"first")
    assert first.path.read_bytes() == b"corrupt"


def test_cache_put_compensates_its_new_object_when_relational_write_fails(
    isolated_services: ApiServices,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = _cache_storage(isolated_services, tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"original")
    original = service.put("build", "payload", source)
    source.write_bytes(b"replacement")

    def reject_upsert(_repository: CacheEntryRepository, _record: CacheEntry) -> CacheEntry:
        raise RuntimeError("injected relational write failure")

    monkeypatch.setattr(CacheEntryRepository, "upsert", reject_upsert)

    with pytest.raises(RuntimeError, match="injected relational write failure"):
        service.put("build", "payload", source)

    with isolated_services.context.database.session() as session:
        assert CacheEntryRepository(session).get(original.key) == original
    assert list(client.iter_paths()) == [Path(original.path)]
    assert Path(original.path).read_bytes() == b"original"


def test_cache_put_retires_the_previous_object_after_pointer_commit(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    service, client = _cache_storage(isolated_services, tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"first")
    first = service.put("build", "payload", source)

    source.write_bytes(b"second")
    second = service.put("build", "payload", source)

    assert first.key == second.key
    assert first.path != second.path
    assert Path(first.path).exists() is False
    assert Path(second.path).read_bytes() == b"second"
    assert Path(second.path).name == f"{second.key}{second.sha256}"
    assert list(client.iter_paths()) == [Path(second.path)]


def test_failed_old_object_retirement_is_reconciled_as_an_orphan(
    isolated_services: ApiServices,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _FailOnceDeleteCacheClient(MountedCacheSettings(root=tmp_path / "mounted-cache"))
    service, _ = _cache_storage(isolated_services, tmp_path, client=client)
    source = tmp_path / "source"
    source.write_bytes(b"first")
    first = service.put("build", "payload", source)
    client.fail_path = Path(first.path)

    source.write_bytes(b"second")
    replacement = service.put("build", "payload", source)

    current = service.list()
    assert len(current) == 1
    assert current == [replacement]
    assert current[0].sha256 == hashlib.sha256(b"second").hexdigest()
    assert Path(current[0].path).read_bytes() == b"second"
    assert Path(first.path).read_bytes() == b"first"
    assert "cache old-object retirement deferred" in caplog.text

    result = service.reconcile(limit=10)

    assert result.records_removed == 0
    assert result.objects_removed == 1
    assert Path(first.path).exists() is False


def test_cache_delete_leaves_row_as_retry_evidence_until_bytes_are_removed(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    client = _FailOnceDeleteCacheClient(MountedCacheSettings(root=tmp_path / "mounted-cache"))
    service, _ = _cache_storage(isolated_services, tmp_path, client=client)
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    record = service.put("build", "payload", source)
    client.fail_path = Path(record.path)

    with pytest.raises(OSError, match="injected cache byte deletion failure"):
        service.delete("build", "payload")

    with isolated_services.context.database.session() as session:
        retry_record = CacheEntryRepository(session).get(record.key)
    assert retry_record is not None
    assert retry_record.hits == 0
    assert Path(retry_record.path).read_bytes() == b"payload"

    service.delete("build", "payload")

    with isolated_services.context.database.session() as session:
        assert CacheEntryRepository(session).get(record.key) is None
    assert Path(record.path).exists() is False


def test_cache_row_delete_failure_is_retryable_after_bytes_are_gone(
    isolated_services: ApiServices,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _cache_storage(isolated_services, tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    record = service.put("build", "payload", source)

    def reject_delete(_repository: CacheEntryRepository, _key: str) -> bool:
        raise RuntimeError("injected relational delete failure")

    with monkeypatch.context() as patch:
        patch.setattr(CacheEntryRepository, "delete", reject_delete)
        with pytest.raises(RuntimeError, match="injected relational delete failure"):
            service.delete("build", "payload")

    with isolated_services.context.database.session() as session:
        assert CacheEntryRepository(session).get(record.key) is not None
    assert Path(record.path).exists() is False

    result = service.reconcile(limit=10)

    assert result.records_removed == 1
    with isolated_services.context.database.session() as session:
        assert CacheEntryRepository(session).get(record.key) is None


def test_cache_reconciliation_removes_missing_records_and_orphan_bytes(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    service, client = _cache_storage(isolated_services, tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"missing")
    missing = service.put("build", "missing", source)
    source.write_bytes(b"retained")
    retained = service.put("build", "retained", source)
    Path(missing.path).unlink()
    orphan = client.put_bytes(
        f"{'f' * 64}{hashlib.sha256(b'orphan').hexdigest()}",
        b"orphan",
    )

    result = service.reconcile(limit=10)

    assert result.records_removed == 1
    assert result.objects_removed == 1
    with isolated_services.context.database.session() as session:
        repository = CacheEntryRepository(session)
        assert repository.get(missing.key) is None
        assert repository.get(retained.key) == retained
    assert orphan.path.exists() is False
    assert Path(retained.path).read_bytes() == b"retained"


def test_cache_reconciliation_removes_abandoned_staging_file(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    service, client = _cache_storage(isolated_services, tmp_path)
    object_key = f"{'e' * 64}{hashlib.sha256(b'staged').hexdigest()}"
    target = client.path_for_key(object_key)
    target.parent.mkdir(parents=True)
    abandoned = target.with_name(f".{target.name}.1234.abandoned.tmp")
    abandoned.write_bytes(b"staged")

    result = service.reconcile(limit=10)

    assert result.records_removed == 0
    assert result.objects_removed == 1
    assert abandoned.exists() is False


def test_cache_reconciliation_scans_past_valid_first_candidates(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    service, client = _cache_storage(isolated_services, tmp_path)
    old_source = tmp_path / "old-source"
    old_source.write_bytes(b"missing")
    missing = service.put("build", "old-missing", old_source)
    Path(missing.path).unlink()

    referenced_key = "0" * 64
    referenced_bytes = b"referenced"
    referenced_object = client.put_bytes(
        f"{referenced_key}{hashlib.sha256(referenced_bytes).hexdigest()}",
        referenced_bytes,
    )
    referenced = CacheEntry(
        key=referenced_key,
        path=str(referenced_object.path),
        size=len(referenced_bytes),
        sha256=hashlib.sha256(referenced_bytes).hexdigest(),
    )
    with isolated_services.context.database.session() as session:
        CacheEntryRepository(session).upsert(referenced)

    orphan_bytes = b"orphan"
    orphan = client.put_bytes(
        f"{'f' * 64}{hashlib.sha256(orphan_bytes).hexdigest()}",
        orphan_bytes,
    )

    result = service.reconcile(limit=1)

    assert result.records_removed == 1
    assert result.objects_removed == 1
    with isolated_services.context.database.session() as session:
        repository = CacheEntryRepository(session)
        assert repository.get(missing.key) is None
        assert repository.get(referenced.key) == referenced
    assert referenced_object.path.read_bytes() == referenced_bytes
    assert orphan.path.exists() is False


def test_concurrent_cache_puts_leave_one_matching_pointer_and_object(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    service, client = _cache_storage(isolated_services, tmp_path)
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    barrier = Barrier(2)

    def publish(source: Path) -> CacheEntry:
        barrier.wait()
        return service.put("build", "concurrent", source)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(publish, (first_source, second_source)))

    current = service.list()
    assert len(results) == 2
    assert len(current) == 1
    data = Path(current[0].path).read_bytes()
    assert current[0].sha256 == hashlib.sha256(data).hexdigest()
    assert data in {b"first", b"second"}
    assert list(client.iter_paths()) == [Path(current[0].path)]
