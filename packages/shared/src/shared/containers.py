from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.container_requests import StopContainerReason
from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.timestamps import utc_now


class ContainerStatus(StringEnum):
    Pending = "pending"
    Running = "running"
    Exited = "exited"
    Failed = "failed"
    Stopped = "stopped"


LIVE_CONTAINER_STATUSES: tuple[ContainerStatus, ...] = (
    ContainerStatus.Pending,
    ContainerStatus.Running,
)
"""The statuses in which a container is holding capacity.

`Pending` counts. A container waiting for a worker has already been promised the
capacity it asked for, and leaving it out would let an account queue past a
concurrency limit and take every slot the moment hardware frees up — a limit
enforced only against work that already started is not one.

Named once because two places read it and they must not drift: what a limit
counts and what a shutdown sweep stops have to be the same set, or an account is
refused for holding containers nothing will ever stop.
"""

TERMINAL_CONTAINER_STATUSES: frozenset[ContainerStatus] = frozenset(
    {
        ContainerStatus.Exited,
        ContainerStatus.Failed,
        ContainerStatus.Stopped,
    }
)
"""The statuses a container never leaves.

The complement of `LIVE_CONTAINER_STATUSES`, and the point past which every path
that settles what a container was holding has already run. Work bound to a
container in one of these is work nothing else will come back for.
"""


class ContainerRecord(ContractModel):
    id: str
    name: str
    image: str
    command: list[str]
    workspace_id: str
    stub_id: str | None = None
    app_id: str | None = None
    machine_id: str | None = None
    worker_id: str | None = None
    runtime_machine_id: str = ""
    runtime_worker_id: str = ""
    task_id: str | None = None
    status: ContainerStatus = ContainerStatus.Pending
    pid: int | None = None
    exit_code: int | None = None
    termination_reason: StopContainerReason = StopContainerReason.Unknown
    startup_error: str = ""
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    ports: dict[str, int] = Field(default_factory=dict)
    network_blocked: bool = False
    network_allow_list: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=0, ge=-1)
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    preemption_settled_at: datetime | None = None


__all__ = ["LIVE_CONTAINER_STATUSES", "ContainerRecord", "ContainerStatus"]
