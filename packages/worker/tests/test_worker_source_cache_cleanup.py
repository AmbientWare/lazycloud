from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from shared.source_cache_cleanup import (
    SourceCacheCleanupStatus,
    SourceCacheCleanupTargetRecord,
)
from worker.execution import stub_code_cache_key
from worker.source_cache_cleanup import (
    SOURCE_CACHE_GENERATION_MARKER,
    WorkerSourceCacheClaimSource,
    WorkerSourceCacheIdentity,
    WorkerSourceCacheReconciler,
    destroy_source_cache_storage,
    record_source_cache_session,
    source_cache_destruction_receipt,
)
from worker.source_code import SourceCodePackageMaterializer


def test_source_cache_marker_is_reused_and_replacement_gets_new_identity(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    first = WorkerSourceCacheIdentity.open(first_root, storage_id="machine:machine-a")
    restarted = WorkerSourceCacheIdentity.open(first_root, storage_id="machine:machine-a")
    replacement = WorkerSourceCacheIdentity.open(
        tmp_path / "replacement",
        storage_id="machine:machine-b",
    )

    assert restarted == first
    assert first.storage_id == "machine:machine-a"
    assert replacement.generation_id != first.generation_id


def test_source_cache_marker_concurrent_open_installs_one_identity(tmp_path: Path) -> None:
    root = tmp_path / "cache"

    def open_identity(_: int) -> WorkerSourceCacheIdentity:
        return WorkerSourceCacheIdentity.open(root, storage_id="node:node-a")

    with ThreadPoolExecutor(max_workers=8) as executor:
        identities = list(executor.map(open_identity, range(32)))

    assert {identity.generation_id for identity in identities} == {identities[0].generation_id}
    assert not list(root.glob(f"{SOURCE_CACHE_GENERATION_MARKER}.*"))


def test_machine_cache_destruction_requires_bound_session_and_removes_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    identity = WorkerSourceCacheIdentity.open(root, storage_id="machine:machine-a")
    (root / "workspace-data").write_text("sensitive", encoding="utf-8")
    record_source_cache_session(root, identity, session_fence=7)

    receipt = source_cache_destruction_receipt(root, storage_id="machine:machine-a")

    assert receipt is not None
    assert receipt.generation_id == identity.generation_id
    assert receipt.storage_id == identity.storage_id
    assert receipt.session_fence == 7
    destroy_source_cache_storage(root, receipt)
    assert not root.exists()


@pytest.mark.parametrize(
    "case",
    [
        "session-binding",
        "storage-empty",
        "storage-name",
        "storage-pod",
        "marker-empty",
        "marker-malformed",
        "marker-symlink",
        "marker-directory",
        "missing-identity",
    ],
)
def test_source_cache_identity_fails_closed_for_unsafe_marker_and_owner_matrix(
    tmp_path: Path,
    case: str,
) -> None:
    root = tmp_path / "cache"
    if case == "session-binding":
        identity = WorkerSourceCacheIdentity.open(root, storage_id="machine:machine-a")
        with pytest.raises(RuntimeError, match="session marker is missing"):
            source_cache_destruction_receipt(root, storage_id=identity.storage_id)
        record_source_cache_session(root, identity, session_fence=2)
        with pytest.raises(RuntimeError, match="does not match"):
            source_cache_destruction_receipt(root, storage_id="machine:machine-b")
        assert root.exists()
        return
    if case.startswith("storage-"):
        storage_id = {
            "storage-empty": "",
            "storage-name": "pod-a",
            "storage-pod": "pod:pod-a",
        }[case]
        with pytest.raises(RuntimeError, match="storage id must identify a machine or node owner"):
            WorkerSourceCacheIdentity.open(root, storage_id=storage_id)
        assert not root.exists()
        return

    root.mkdir()
    marker = root / SOURCE_CACHE_GENERATION_MARKER
    if case == "marker-empty":
        marker.write_text("", encoding="utf-8")
    elif case == "marker-malformed":
        marker.write_text("not-a-uuid", encoding="utf-8")
    elif case == "marker-symlink":
        target = tmp_path / "generation"
        target.write_text("13f4ff1a-2562-47e7-9f08-964588090ee0", encoding="utf-8")
        marker.symlink_to(target)
    elif case == "marker-directory":
        marker.mkdir()
    else:
        cached_source = root / stub_code_cache_key("workspace-a", "source-a")
        cached_source.mkdir(parents=True)
        (cached_source / "payload.py").write_text("sensitive", encoding="utf-8")

    message = (
        "missing from non-empty cache root"
        if case == "missing-identity"
        else "invalid source cache generation marker"
    )
    with pytest.raises(RuntimeError, match=message):
        WorkerSourceCacheIdentity.open(root, storage_id="node:node-a")


def test_source_cache_reconciliation_purges_exact_target_and_preserves_sibling(
    tmp_path: Path,
) -> None:
    materializer = SourceCodePackageMaterializer(cache_root=tmp_path / "cache")
    target_path = materializer.cache_root / stub_code_cache_key("workspace-a", "source-a")
    sibling_path = materializer.cache_root / stub_code_cache_key("workspace-b", "source-b")
    target_path.mkdir(parents=True)
    sibling_path.mkdir(parents=True)
    (target_path / "payload.py").write_text("target", encoding="utf-8")
    (sibling_path / "payload.py").write_text("sibling", encoding="utf-8")
    repository = _CleanupRepository(targets=[_target()])

    result = WorkerSourceCacheReconciler(repository, materializer).reconcile()

    assert result.claimed_count == 1
    assert result.completed_count == 1
    assert result.failed_count == 0
    assert not target_path.exists()
    assert sibling_path.exists()
    assert repository.completed == ["target-1"]
    assert repository.failed == []
    assert repository.activations == 1


def _target() -> SourceCacheCleanupTargetRecord:
    now = datetime.now(UTC)
    return SourceCacheCleanupTargetRecord(
        id="target-1",
        workspace_id="workspace-a",
        cache_generation_id="generation-1",
        source_object_id="source-a",
        status=SourceCacheCleanupStatus.Claimed,
        attempt_count=1,
        next_attempt_at=now,
        claim_token="claim-1",
        claim_expires_at=now,
        claim_session_fence=1,
        created_at=now,
        updated_at=now,
    )


@dataclass(slots=True)
class _CleanupRepository:
    targets: list[SourceCacheCleanupTargetRecord]
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    activations: int = 0

    def claim_source_cache_cleanup(self, *, limit: int) -> WorkerSourceCacheClaimSource:
        claimed = self.targets[:limit]
        del self.targets[:limit]
        return WorkerSourceCacheClaimSource(targets=claimed)

    def complete_source_cache_cleanup(self, target: SourceCacheCleanupTargetRecord) -> None:
        self.completed.append(target.id)

    def fail_source_cache_cleanup(self, target: SourceCacheCleanupTargetRecord) -> None:
        self.failed.append(target.id)

    def activate_source_cache(self) -> None:
        self.activations += 1
