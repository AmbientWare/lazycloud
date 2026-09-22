from pathlib import Path

import pytest
from agent.operations import build_agent_worker_dirs
from agent.storage_cleanup import prepare_machine_storage_for_stop, read_stop_preparation
from worker.source_cache_cleanup import WorkerSourceCacheIdentity, record_source_cache_session


def test_stop_cleanup_preserves_agent_identity_and_fences_the_removed_cache(tmp_path: Path) -> None:
    state = tmp_path / "agent"
    state.mkdir()
    (state / "state.json").write_text("retained agent identity")
    directories = build_agent_worker_dirs(str(state), "worker-1")
    for name in directories.all_paths():
        directory = Path(name)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tenant-data").write_text("tenant-owned contents")
    cache = state / "cache" / "source-code"
    identity = WorkerSourceCacheIdentity.open(cache, storage_id="machine:machine-1")
    record_source_cache_session(cache, identity, session_fence=7)

    receipt = prepare_machine_storage_for_stop(
        state, machine_id="machine-1", worker_id="worker-1", request_id="stop-1"
    )

    assert receipt.cache_generation_id == identity.generation_id
    assert receipt.cache_session_fence == 7
    assert all(not Path(path).exists() for path in directories.all_paths())
    assert (state / "state.json").read_text() == "retained agent identity"
    assert read_stop_preparation(state) == receipt
    assert (
        prepare_machine_storage_for_stop(
            state, machine_id="machine-1", worker_id="worker-1", request_id="stop-1"
        )
        == receipt
    )
    with pytest.raises(RuntimeError, match="another machine lifecycle"):
        prepare_machine_storage_for_stop(
            state, machine_id="machine-1", worker_id="worker-1", request_id="stop-2"
        )


def test_stop_cleanup_refuses_a_tenant_path_outside_the_agent_directory(tmp_path: Path) -> None:
    state = tmp_path / "agent"
    state.mkdir()
    sibling = tmp_path / "other-agent"
    sibling.mkdir()
    (sibling / "keep").write_text("unrelated user data")
    (state / "cache").symlink_to(sibling, target_is_directory=True)

    with pytest.raises(RuntimeError, match="leaves the agent state directory"):
        prepare_machine_storage_for_stop(
            state, machine_id="machine-1", worker_id="worker-1", request_id="stop-1"
        )
    assert (sibling / "keep").read_text() == "unrelated user data"
