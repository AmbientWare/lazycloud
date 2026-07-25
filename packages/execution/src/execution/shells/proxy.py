from __future__ import annotations

from dataclasses import dataclass

from shared.scheduling import SchedulerBackendRoute


@dataclass(frozen=True, slots=True)
class ShellBackendTarget:
    container_id: str
    stub_id: str
    address: str
    route: SchedulerBackendRoute | None
    worker_port: int
    buffer_size_bytes: int
    dial_timeout_seconds: int
