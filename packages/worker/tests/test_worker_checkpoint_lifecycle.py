from __future__ import annotations

from worker.checkpoints import (
    CheckpointArchiveMaterializationRequest,
    CheckpointMaterializationAction,
    plan_checkpoint_archive_materialization,
)


def test_checkpoint_archive_materialization_rejects_incomplete_and_unavailable_sources() -> None:
    incomplete = plan_checkpoint_archive_materialization(
        CheckpointArchiveMaterializationRequest(
            checkpoint_id="chk",
            checkpoint_root="/checkpoints",
            cache_hash="",
            cache_size_bytes=0,
            origin_key="",
        )
    )
    unavailable = plan_checkpoint_archive_materialization(
        CheckpointArchiveMaterializationRequest(
            checkpoint_id="chk",
            checkpoint_root="/checkpoints",
            cache_hash="hash",
            cache_size_bytes=12,
            origin_key="checkpoints/chk.tar",
            cache_available=False,
            origin_storage_available=False,
        )
    )

    assert incomplete.action is CheckpointMaterializationAction.Reject
    assert "metadata is incomplete" in incomplete.error_message
    assert unavailable.action is CheckpointMaterializationAction.Reject
    assert "unavailable" in unavailable.error_message
