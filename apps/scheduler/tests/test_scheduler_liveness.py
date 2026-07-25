from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Never

import pytest
from scheduler.service import Scheduler
from scheduler_app.main import run_scheduler
from scheduler_app.runtime import SchedulerRuntime


def test_run_scheduler_beats_heartbeat_file_each_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_file = tmp_path / "scheduler.heartbeat"
    scheduler = Scheduler(interval_seconds=0.0)

    def run_once(
        *,
        now: datetime | None = None,
        include_cron_jobs: bool = True,
        include_containers: bool = True,
        include_container_dispatch: bool = True,
        container_limit: int = 100,
    ) -> Never:
        del now, include_cron_jobs, include_containers, include_container_dispatch, container_limit
        raise KeyboardInterrupt

    monkeypatch.setattr(scheduler, "run_once", run_once)

    with pytest.raises(KeyboardInterrupt):
        run_scheduler(
            runtime=SchedulerRuntime(scheduler=scheduler),
            heartbeat_file=heartbeat_file,
            include_containers=False,
        )

    assert heartbeat_file.exists()
