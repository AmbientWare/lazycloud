from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

RUNTIME_DIR_ENV = "LAZYCLOUD_TEST_RUNTIME_DIR"
TESTS_DIR = Path(__file__).resolve().parent


@pytest.mark.parametrize("version", ["3.10", "3.11", "3.12", "3.13", "3.14"])
def test_managed_runtime_serves_the_protocol_in_a_slim_image(version: str) -> None:
    """Opt-in: set LAZYCLOUD_TEST_RUNTIME_DIR to a deploy/local/build-runtime.sh output."""

    root = os.environ.get(RUNTIME_DIR_ENV)
    if not root:
        pytest.skip(f"{RUNTIME_DIR_ENV} names no built runtime")
    runtime = Path(root).resolve() / version
    if not runtime.is_dir():
        pytest.skip(f"no runtime built for Python {version}")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--tmpfs=/tmp",
            f"--volume={runtime}:/opt/lazycloud/runtime:ro",
            "--env=PYTHONPATH=/opt/lazycloud/runtime",
            f"--volume={TESTS_DIR}:/workspace:ro",
            "--workdir=/workspace",
            f"python:{version}-slim",
            "python3",
            "runtime_probe.py",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith(f"runtime probe ok: python {version}."), result.stdout
