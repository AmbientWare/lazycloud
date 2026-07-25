from pathlib import Path

import pytest

from runner import checkpoints


def test_runner_checkpoint_handshake_waits_and_loads_restored_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkpoints, "CHECKPOINT_SIGNAL_DIR", tmp_path)
    monkeypatch.setattr(checkpoints, "CHECKPOINT_READY_FILE", tmp_path / "READY_FOR_CHECKPOINT")
    monkeypatch.setattr(
        checkpoints,
        "CHECKPOINT_COMPLETE_FILE",
        tmp_path / "CHECKPOINT_COMPLETE",
    )
    monkeypatch.setattr(
        checkpoints,
        "CHECKPOINT_CONTAINER_ID_FILE",
        tmp_path / "CONTAINER_ID",
    )
    monkeypatch.setattr(
        checkpoints,
        "CHECKPOINT_CONTAINER_HOSTNAME_FILE",
        tmp_path / "CONTAINER_HOSTNAME",
    )
    (tmp_path / "CONTAINER_ID").write_text("container-restored")
    (tmp_path / "CONTAINER_HOSTNAME").write_text("host-restored")
    (tmp_path / "CHECKPOINT_COMPLETE").touch()

    identity = checkpoints.wait_for_checkpoint(
        enabled=True,
        workers=1,
        poll_interval_seconds=0.001,
    )

    assert identity is not None
    assert identity.container_id == "container-restored"
    assert identity.container_hostname == "host-restored"
    assert (tmp_path / "READY_FOR_CHECKPOINT").is_file()
