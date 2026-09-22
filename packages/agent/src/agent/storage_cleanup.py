from __future__ import annotations

import os
import shutil
from pathlib import Path

from pydantic import ValidationError
from shared.compute_enrollment import MachineStopPreparationReceipt
from worker.source_cache_cleanup import (
    WorkerSourceCacheDestructionReceipt,
    destroy_source_cache_storage,
    source_cache_destruction_receipt,
)

from agent.operations import AGENT_SOURCE_CACHE_RELATIVE_PATH, build_agent_worker_dirs

SOURCE_CACHE_DESTRUCTION_RECEIPT_FILE = "source-cache-destruction.json"
STOP_PREPARATION_RECEIPT_FILE = "stop-preparation.json"


def _write_receipt(path: Path, payload: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            temporary.chmod(0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_source_cache_destruction(
    state_dir: Path, machine_id: str
) -> WorkerSourceCacheDestructionReceipt | None:
    receipt_path = state_dir / SOURCE_CACHE_DESTRUCTION_RECEIPT_FILE
    storage_id = f"machine:{machine_id}"
    cache_root = state_dir / AGENT_SOURCE_CACHE_RELATIVE_PATH
    if receipt_path.exists():
        try:
            receipt = WorkerSourceCacheDestructionReceipt.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise RuntimeError("source cache destruction receipt is invalid") from exc
        if receipt.storage_id != storage_id:
            raise RuntimeError("source cache destruction receipt belongs to another machine")
    else:
        receipt = source_cache_destruction_receipt(cache_root, storage_id=storage_id)
        if receipt is None:
            return None
        _write_receipt(receipt_path, receipt.model_dump_json())
    destroy_source_cache_storage(cache_root, receipt)
    return receipt


def read_stop_preparation(state_dir: Path) -> MachineStopPreparationReceipt | None:
    path = state_dir / STOP_PREPARATION_RECEIPT_FILE
    if not path.exists():
        return None
    return MachineStopPreparationReceipt.model_validate_json(path.read_text(encoding="utf-8"))


def prepare_machine_storage_for_stop(
    state_dir: Path, *, machine_id: str, worker_id: str, request_id: str
) -> MachineStopPreparationReceipt:
    """Remove machine-owned tenant files after its worker processes have stopped."""
    root = state_dir.resolve(strict=True)
    if root == Path("/") or root == Path.home() or state_dir.is_symlink():
        raise ValueError("invalid agent state directory for reserve cleanup")
    prior = read_stop_preparation(root)
    if prior is not None:
        if prior.request_id != request_id:
            raise RuntimeError("stop preparation belongs to another machine lifecycle")
        return prior
    owned = tuple(Path(path) for path in build_agent_worker_dirs(str(root), worker_id).all_paths())
    mounts = [line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    for path in owned:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise RuntimeError("tenant storage path leaves the agent state directory")
        if any(mount == str(path) or mount.startswith(f"{path}/") for mount in mounts):
            raise RuntimeError("tenant storage still has mounted filesystems")
    cache = prepare_source_cache_destruction(root, machine_id)
    for path in owned:
        if path.exists():
            shutil.rmtree(path)
    receipt = MachineStopPreparationReceipt(
        request_id=request_id,
        cache_generation_id=cache.generation_id if cache else "",
        cache_session_fence=cache.session_fence if cache else None,
    )
    _write_receipt(root / STOP_PREPARATION_RECEIPT_FILE, receipt.model_dump_json())
    return receipt


def finish_stop_preparation(state_dir: Path) -> None:
    (state_dir / STOP_PREPARATION_RECEIPT_FILE).unlink(missing_ok=True)
    (state_dir / SOURCE_CACHE_DESTRUCTION_RECEIPT_FILE).unlink(missing_ok=True)
