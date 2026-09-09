from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
from worker.events import ContainerRequestContext, WorkerPoolMode, WorkerUsageEvidence
from worker.image_build_metering import ImageBuildMetering
from worker.image_build_resources import ImageBuildResources
from worker.supervision import WorkerUsageEmissionResult


@dataclass
class _Recorder:
    failures_remaining: int
    accepted: list[WorkerUsageEvidence] = field(default_factory=list)

    def record_usage_window(
        self,
        request: ContainerRequestContext,
        *,
        duration_ms: int,
        window_start_ms: int = 0,
        window_end_ms: int | None = None,
        metering_window_started_at: datetime,
        metering_window_ended_at: datetime,
        evidence: WorkerUsageEvidence | None = None,
        measurement_complete: bool = False,
    ) -> WorkerUsageEmissionResult:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("usage API unavailable")
        assert evidence is not None
        result = WorkerUsageEmissionResult(
            worker_id="worker-1",
            container_id=request.container_id,
            duration_ms=duration_ms,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms or window_start_ms + duration_ms,
            metering_window_started_at=metering_window_started_at,
            metering_window_ended_at=metering_window_ended_at,
            pool_mode=WorkerPoolMode.Public,
        )
        self.accepted.append(evidence)
        return result


@pytest.mark.parametrize("failures", [2, 100])
def test_build_final_usage_retries_end_without_leaking_publisher(
    tmp_path: Path, failures: int
) -> None:
    (tmp_path / "cpu.stat").write_text("usage_usec 0\n")
    (tmp_path / "memory.stat").write_text("anon 0\nfile_mapped 0\n")
    recorder = _Recorder(failures)
    metering = ImageBuildMetering(
        ImageBuildResources(tmp_path), ContainerRequestContext(container_id="build-1"), recorder
    )
    metering.start()
    (tmp_path / "cpu.stat").write_text("usage_usec 500000\n")

    metering.close()

    assert metering._publisher is not None and not metering._publisher.is_alive()
    assert [evidence.cpu_used_core_seconds for evidence in recorder.accepted] == (
        [0.5] if failures == 2 else []
    )


def test_build_final_counter_failure_still_stops_publisher(tmp_path: Path) -> None:
    (tmp_path / "cpu.stat").write_text("usage_usec 0\n")
    (tmp_path / "memory.stat").write_text("anon 0\nfile_mapped 0\n")
    metering = ImageBuildMetering(
        ImageBuildResources(tmp_path), ContainerRequestContext(container_id="build-2"), _Recorder(0)
    )
    metering.start()
    (tmp_path / "cpu.stat").unlink()

    with pytest.raises(FileNotFoundError):
        metering.close()

    assert metering._publisher is not None and not metering._publisher.is_alive()
