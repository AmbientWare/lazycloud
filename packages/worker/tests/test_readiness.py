from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

import pytest
from worker.gpu import NvidiaGpuIndexProvider


def test_a_stuck_preparation_does_not_keep_the_worker_alive() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            dedent("""\
                from threading import Event
                from worker.readiness import WorkerReadiness

                entered = Event()

                def blocked():
                    entered.set()
                    Event().wait()

                readiness = WorkerReadiness(preparation_checks={"runtime": blocked})
                readiness.start()
                assert entered.wait(2)
                readiness.close(timeout_seconds=0)
                """),
        ],
        check=True,
        timeout=5,
    )


def test_gpu_readiness_bounds_an_unresponsive_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = tmp_path / "nvidia-smi"
    command.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    command.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    began = time.monotonic()
    with pytest.raises(RuntimeError):
        NvidiaGpuIndexProvider().require_devices(1, timeout_seconds=0.1)
    assert time.monotonic() - began < 2
