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
    request_owner_user_id: str,
) -> None:
    """Refuse a request this worker is not entitled to run.

    Placement decides: a request lands only where its own placement is, and the
    worker's placement was stamped from its unit at registration, never from what
    the worker reported. A private worker additionally answers only to the account
    that owns it, which its machine's enrollment named.
    """

    if request.placement != worker.placement:
        msg = f"worker {worker.worker_id} is not in placement {request.placement}"
        raise WorkerRequestNotAdmissibleError(msg)
    if (
        principal is not None
        and principal.is_private_worker
        and (not worker.owner_user_id or worker.owner_user_id != request_owner_user_id)
    ):
        msg = f"worker {worker.worker_id} does not belong to the request's account"
        raise WorkerRequestNotAdmissibleError(msg)


__all__ = ["WorkerRequestNotAdmissibleError", "require_admissible_worker_request"]
