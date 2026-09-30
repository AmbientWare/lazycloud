from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lazycloud.abstractions.workspace_sync import ContainerWorkspaceSyncer
from lazycloud.terminal import Terminal
from shared.http.workspace_sync import (
    WorkspaceSyncBatch,
    WorkspaceSyncOperation,
    WorkspaceSyncResponse,
)


@dataclass
class RecordingSyncClient:
    sync_requests: list[WorkspaceSyncBatch] = field(default_factory=list)

    def sync_container_workspace(self, body: WorkspaceSyncBatch) -> WorkspaceSyncResponse:
        self.sync_requests.append(body.model_copy(update={"data": tuple(body.data)}))
        return WorkspaceSyncResponse(applied=len(body.manifest.entries))


def test_workspace_syncer_writes_filtered_tree_through_gateway(tmp_path: Path) -> None:
    gateway = RecordingSyncClient()
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "__pycache__").mkdir()
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "pkg" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "ignored.py").write_text("raise RuntimeError\n", encoding="utf-8")
    (source / "__pycache__" / "skip.py").write_text("skip\n", encoding="utf-8")
    (source / ".lazycloudignore").write_text(
        ".lazycloudignore\nignored.py\n__pycache__\n",
        encoding="utf-8",
    )

    ContainerWorkspaceSyncer(
        container_id="ctr-serve",
        local_dir=str(source),
        gateway_client=gateway,
        terminal=Terminal(quiet=True),
    ).sync_once()

    writes = {
        entry.path: entry for request in gateway.sync_requests for entry in request.manifest.entries
    }
    assert writes["app.py"].operation is WorkspaceSyncOperation.Write
    assert b"".join(gateway.sync_requests[0].data) == b"print('ok')\nVALUE = 1\n"
    assert "ignored.py" not in writes
    assert "__pycache__/skip.py" not in writes
