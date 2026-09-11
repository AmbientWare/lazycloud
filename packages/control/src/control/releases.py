from __future__ import annotations

from dataclasses import dataclass, field

from shared.errors import ConflictError
from shared.releases import ActiveRelease
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus

from control.release_settings import ReleaseSettings


@dataclass(slots=True)
class DeploymentReleaseService:
    settings: ReleaseSettings = field(default_factory=ReleaseSettings)

    def active(self) -> ActiveRelease | None:
        try:
            release = ActiveRelease.model_validate_json(self.settings.active_file.read_bytes())
        except FileNotFoundError:
            return None
        return release

    def controls(self, release: ActiveRelease) -> bool:
        return release.manifest_url == self.settings.manifest_url

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
            or (
                worker.status is SchedulerWorkerStatus.Available
                and 0 < worker.admitted_release_generation <= release.generation
            )
        ]

    def worker_registration_generation(self, worker: SchedulerWorkerRecord) -> int:
        release = self.active()
        if release is None or not release.admits(worker.runtime_image, worker.agent_binary_sha256):
            return 0
        return release.generation


__all__ = ["DeploymentReleaseService"]
