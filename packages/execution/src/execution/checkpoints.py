from __future__ import annotations

from database.repositories.images import CheckpointRepository
from shared.checkpoints import CheckpointRecord

from execution.context import ExecutionContext


def latest_available_checkpoint(
    context: ExecutionContext,
    *,
    stub_id: str,
    workspace_id: str,
) -> CheckpointRecord | None:
    with context.database.session() as session:
        return CheckpointRepository(session).latest_available_for_stub(
            workspace_id=workspace_id,
            stub_id=stub_id,
        )


__all__ = ["latest_available_checkpoint"]
