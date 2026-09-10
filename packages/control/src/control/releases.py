from __future__ import annotations

from dataclasses import dataclass, field

from shared.errors import ConflictError
from shared.releases import ActiveRelease
from shared.scheduling import SchedulerWorkerRecord

from control.release_settings import ReleaseSettings


@dataclass(slots=True)
class DeploymentReleaseService:
    settings: ReleaseSettings = field(default_factory=ReleaseSettings)

    def active(self) -> ActiveRelease | None:
        try:
            release = ActiveRelease.model_validate_json(self.settings.active_file.read_bytes())
        except FileNotFoundError:
            return None
        # A rolling replica must never issue commands for another build's release.
        return release if release.manifest_url == self.settings.manifest_url else None

    def state(self) -> ActiveRelease:
        release = self.active()
        if release is None:
            raise ConflictError("platform release is not active yet")
        return release

    def admitted_workers(self, workers: list[SchedulerWorkerRecord]) -> list[SchedulerWorkerRecord]:
        release = self.active()
        if release is None:
            return []
        return [
            worker
            for worker in workers
            if release.admits(worker.runtime_image, worker.agent_binary_sha256)
        ]


__all__ = ["DeploymentReleaseService"]
