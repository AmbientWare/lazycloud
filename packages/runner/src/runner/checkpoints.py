from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

CHECKPOINT_SIGNAL_DIR = Path("/criu")
CHECKPOINT_READY_FILE = CHECKPOINT_SIGNAL_DIR / "READY_FOR_CHECKPOINT"
CHECKPOINT_COMPLETE_FILE = CHECKPOINT_SIGNAL_DIR / "CHECKPOINT_COMPLETE"
CHECKPOINT_CONTAINER_ID_FILE = CHECKPOINT_SIGNAL_DIR / "CONTAINER_ID"
CHECKPOINT_CONTAINER_HOSTNAME_FILE = CHECKPOINT_SIGNAL_DIR / "CONTAINER_HOSTNAME"


@dataclass(frozen=True, slots=True)
class RestoredContainerIdentity:
    container_id: str
    container_hostname: str


def wait_for_checkpoint(
    *,
    enabled: bool,
    workers: int = 1,
    poll_interval_seconds: float = 0.1,
) -> RestoredContainerIdentity | None:
    if not enabled:
        return None
    if workers < 1:
        raise ValueError("checkpoint worker count must be positive")
    CHECKPOINT_SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    (CHECKPOINT_SIGNAL_DIR / f"worker-{os.getpid()}.ready").touch(exist_ok=True)
    while len(tuple(CHECKPOINT_SIGNAL_DIR.glob("worker-*.ready"))) < workers:
        time.sleep(poll_interval_seconds)
    CHECKPOINT_READY_FILE.touch(exist_ok=True)
    while not CHECKPOINT_COMPLETE_FILE.is_file():
        time.sleep(poll_interval_seconds)
    return RestoredContainerIdentity(
        container_id=CHECKPOINT_CONTAINER_ID_FILE.read_text(encoding="utf-8").strip(),
        container_hostname=CHECKPOINT_CONTAINER_HOSTNAME_FILE.read_text(encoding="utf-8").strip(),
    )
