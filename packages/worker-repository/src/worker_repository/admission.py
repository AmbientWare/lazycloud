from __future__ import annotations

from shared.errors import ConflictError
from shared.scheduling import (
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
    worker_serves_owner,
)
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

    Tenancy is compared by account. A private worker is one customer's machine and
    serves every workspace that customer owns, so the deciding pair is the account
    stamped on the worker record from its machine's enrollment and the account that
    owns the requesting workspace. Neither is anything the worker reported.
    """

    if request.pool_selector and request.pool_selector != worker.pool:
        msg = f"worker {worker.worker_id} does not belong to requested pool {request.pool_selector}"
        raise WorkerRequestNotAdmissibleError(msg)
    if (
        principal is not None
        and principal.is_private_worker
        and not worker_serves_owner(
            private_worker=True,
            worker_owner_user_id=worker.owner_user_id,
            request_owner_user_id=request_owner_user_id,
        )
    ):
        msg = f"worker {worker.worker_id} does not belong to the request's account"
        raise WorkerRequestNotAdmissibleError(msg)


__all__ = ["WorkerRequestNotAdmissibleError", "require_admissible_worker_request"]
