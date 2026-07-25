"""File-heartbeat liveness for loop processes without a serving socket.

Loop processes such as the task worker and the cache reconciler touch a
heartbeat file once per loop iteration. Container healthchecks and Kubernetes
exec probes run this module as a check command; a missing or stale heartbeat
means the loop is wedged and the probe fails.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_HEARTBEAT_ROOT = Path("/tmp")


class ProcessLivenessArguments(argparse.Namespace):
    path: Path
    max_age_seconds: float


def heartbeat_path(process_name: str) -> Path:
    """Default heartbeat file location for a named process."""
    return _HEARTBEAT_ROOT / f"{process_name}.heartbeat"


@dataclass(frozen=True, slots=True)
class HeartbeatFile:
    """Loop-progress heartbeat recorded as the file's modification time."""

    path: Path

    def beat(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def age_seconds(self, *, now: float | None = None) -> float | None:
        try:
            modified = self.path.stat().st_mtime
        except OSError:
            return None
        current = time.time() if now is None else now
        return max(0.0, current - modified)

    def is_fresh(self, max_age_seconds: float, *, now: float | None = None) -> bool:
        age = self.age_seconds(now=now)
        return age is not None and age <= max_age_seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="process-liveness",
        description="Fail when a loop-process heartbeat file is missing or stale.",
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-age-seconds", type=float, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv, namespace=ProcessLivenessArguments())
    max_age_seconds = args.max_age_seconds
    if max_age_seconds <= 0:
        print("--max-age-seconds must be positive", file=sys.stderr)
        return 2
    heartbeat = HeartbeatFile(args.path)
    age = heartbeat.age_seconds()
    if age is None:
        print(f"heartbeat missing: {args.path}", file=sys.stderr)
        return 1
    if age > max_age_seconds:
        print(
            f"heartbeat stale: {age:.0f}s > {max_age_seconds:.0f}s ({args.path})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
