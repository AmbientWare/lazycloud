from __future__ import annotations

from shared.errors import ConflictError
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerRequest
from worker.repository_payloads import WorkerRepositoryPrincipal


class WorkerRequestNotAdmissibleError(ConflictError):
    """This worker can never run this request.

    Separate from the transient refusals that share the same call path—a cache
    generation still initializing, a worker record not yet readable—because the caller
    must not offer the request to this worker again. Offering it back is how a
    permanent mismatch became a request that never placed, never failed, and reported
    nothing.
    """


def require_admissible_worker_request(
    worker: SchedulerWorkerRecord,
    request: SchedulerWorkerRequest,
    *,
    principal: WorkerRepositoryPrincipal | None,
) -> None:
    """Refuse a request this worker is not entitled to run.

    The workspace is read from the presented credential rather than the worker
    record, because the credential is what the platform issued and the record is
    what the worker reported.
    """

    if request.pool_selector and request.pool_selector != worker.pool:
        msg = f"worker {worker.worker_id} does not belong to requested pool {request.pool_selector}"
        raise WorkerRequestNotAdmissibleError(msg)
    if (
        principal is not None
        and principal.is_private_worker
        and principal.workspace_id != request.workspace_id
    ):
        msg = f"worker {worker.worker_id} does not belong to request workspace"
        raise WorkerRequestNotAdmissibleError(msg)


__all__ = ["WorkerRequestNotAdmissibleError", "require_admissible_worker_request"]
