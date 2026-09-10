from __future__ import annotations

import os
from pathlib import Path

from shared.process_liveness import HeartbeatFile, main


def test_beat_creates_parent_and_refreshes_mtime(tmp_path: Path) -> None:
    heartbeat = HeartbeatFile(tmp_path / "liveness" / "loop.heartbeat")
    assert heartbeat.age_seconds() is None
    heartbeat.beat()
    assert heartbeat.path.exists()
    stale = heartbeat.path.stat().st_mtime - 120
    os.utime(heartbeat.path, (stale, stale))
    heartbeat.beat()
    assert heartbeat.path.stat().st_mtime > stale


def test_freshness_against_reference_clock(tmp_path: Path) -> None:
    heartbeat = HeartbeatFile(tmp_path / "loop.heartbeat")
    heartbeat.beat()
    recorded = heartbeat.path.stat().st_mtime
    assert heartbeat.is_fresh(30.0, now=recorded + 10.0)
    assert not heartbeat.is_fresh(30.0, now=recorded + 31.0)
    assert not HeartbeatFile(tmp_path / "missing").is_fresh(30.0)


def test_check_command_exit_codes(tmp_path: Path) -> None:
    path = tmp_path / "loop.heartbeat"
    assert main([str(path), "--max-age-seconds", "60"]) == 1
    HeartbeatFile(path).beat()
    assert main([str(path), "--max-age-seconds", "60"]) == 0
    stale = path.stat().st_mtime - 120.0
    os.utime(path, (stale, stale))
    assert main([str(path), "--max-age-seconds", "60"]) == 1
    assert main([str(path), "--max-age-seconds", "0"]) == 2
