from __future__ import annotations

import argparse
from enum import StrEnum
from pathlib import Path

from shared.app_identity import SCHEDULER_PROCESS_NAME
from shared.process_liveness import check_heartbeats, heartbeat_path


class SchedulerLoopName(StrEnum):
    Placement = "placement"
    Capacity = "capacity"
    Housekeeping = "housekeeping"
    Dispatch = "dispatch"


def scheduler_heartbeat_paths(base: Path) -> dict[SchedulerLoopName, Path]:
    return {name: base.with_name(f"{base.name}.{name}") for name in SchedulerLoopName}


class SchedulerHealthArguments(argparse.Namespace):
    heartbeat_file: Path
    max_age_seconds: float


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check every scheduler loop's heartbeat.")
    parser.add_argument(
        "--heartbeat-file",
        type=Path,
        default=heartbeat_path(SCHEDULER_PROCESS_NAME),
    )
    parser.add_argument("--max-age-seconds", type=float, required=True)
    args = parser.parse_args(argv, namespace=SchedulerHealthArguments())
    return check_heartbeats(
        scheduler_heartbeat_paths(args.heartbeat_file).values(),
        max_age_seconds=args.max_age_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
