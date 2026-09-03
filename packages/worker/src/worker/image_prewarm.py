from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from shared.image_prewarm import WorkerImagePrewarmArchive

from worker.container_startup import WorkerImagePrewarmResult, WorkerImagePrewarmSourceLoader


class WorkerImagePrewarmRepository(Protocol):
    def list_image_prewarm_targets(
        self,
        worker_id: str,
        *,
        limit: int = 4,
    ) -> list[WorkerImagePrewarmArchive]: ...


class WorkerImagePrewarmLoader(Protocol):
    def prewarm_image(
        self,
        target: WorkerImagePrewarmArchive,
        *,
        source_loader: WorkerImagePrewarmSourceLoader | None = None,
    ) -> WorkerImagePrewarmResult: ...


@dataclass(slots=True)
class WorkerImagePrewarmService:
    worker_id: str
    repository: WorkerImagePrewarmRepository
    loader: WorkerImagePrewarmLoader
    source_loader: WorkerImagePrewarmSourceLoader | None = None
    limit: int = 4

    def reconcile(
        self,
        *,
        limit: int | None = None,
        on_start: Callable[[WorkerImagePrewarmArchive], None] | None = None,
    ) -> list[WorkerImagePrewarmResult]:
        results: list[WorkerImagePrewarmResult] = []
        for target in self.repository.list_image_prewarm_targets(
            self.worker_id,
            limit=self.limit if limit is None else limit,
        ):
            if on_start is not None:
                on_start(target)
            try:
                result = self.loader.prewarm_image(
                    target,
                    source_loader=self.source_loader,
                )
            except Exception as exc:
                result = WorkerImagePrewarmResult(
                    image_id=target.image_id,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
        return results
