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


__all__ = ["ContainerRecord", "ContainerStatus"]
