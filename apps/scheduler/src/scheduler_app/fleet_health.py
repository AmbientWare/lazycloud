from __future__ import annotations

import argparse
from pathlib import Path

from shared.process_liveness import check_heartbeats, heartbeat_path

FLEET_PROCESS_NAME = "lazycloud-fleet-controller"
FLEET_LOOPS = ("acquisition", "capacity", "housekeeping")


def fleet_heartbeat_paths(base: Path) -> dict[str, Path]:
    return {name: base.with_name(f"{base.name}.{name}") for name in FLEET_LOOPS}


class FleetHealthArguments(argparse.Namespace):
    max_age_seconds: float
    heartbeat_file: Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check fleet controller loop progress.")
    parser.add_argument("--heartbeat-file", type=Path, default=heartbeat_path(FLEET_PROCESS_NAME))
    parser.add_argument("--max-age-seconds", type=float, required=True)
    args = parser.parse_args(argv, namespace=FleetHealthArguments())
    return check_heartbeats(
        fleet_heartbeat_paths(args.heartbeat_file).values(), max_age_seconds=args.max_age_seconds
    )


if __name__ == "__main__":
    raise SystemExit(main())
