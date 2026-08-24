from __future__ import annotations

from dataclasses import dataclass

from control.service import ControlPlaneService


@dataclass(frozen=True, slots=True)
class ControlPlaneWorkspaceStorageState:
    """Persists issuer-owned fields onto a workspace's storage config.

    Merged into the bag rather than replacing it, because the issuer owns only the
    keys it minted: the endpoint, region and path style beside them describe where
    the bucket is, and are nobody's to overwrite from here.
    """

    control_plane: ControlPlaneService

    def save(self, *, workspace_id: str, fields: dict[str, str]) -> None:
        workspace = self.control_plane.get_workspace(workspace_id)
        storage = workspace.storage
        self.control_plane.set_workspace_storage(
            workspace_id,
            storage.model_copy(update={"config": {**storage.config, **fields}}),
        )


__all__ = ["ControlPlaneWorkspaceStorageState"]
