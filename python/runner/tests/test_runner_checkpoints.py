from pathlib import Path

import pytest
from runner.function import FunctionRunner, FunctionRunnerConfig
from shared.lifecycle import LifecycleHooks

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


@pytest.mark.usefixtures("isolated_imports")
def test_function_slot_started_after_restore_uses_restored_identity_in_startup_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for constant, name in (
        ("CHECKPOINT_COMPLETE_FILE", "CHECKPOINT_COMPLETE"),
        ("CHECKPOINT_READY_FILE", "READY_FOR_CHECKPOINT"),
        ("CHECKPOINT_CONTAINER_ID_FILE", "CONTAINER_ID"),
        ("CHECKPOINT_CONTAINER_HOSTNAME_FILE", "CONTAINER_HOSTNAME"),
    ):
        monkeypatch.setattr(checkpoints, constant, tmp_path / name)
    monkeypatch.setattr(checkpoints, "CHECKPOINT_SIGNAL_DIR", tmp_path)
    (tmp_path / "CHECKPOINT_COMPLETE").touch()
    (tmp_path / "CONTAINER_ID").write_text("restored-container")
    (tmp_path / "CONTAINER_HOSTNAME").write_text("restored-host")
    (tmp_path / "restored_function.py").write_text(
        "def handler():\n    return None\n\n"
        "def started(context):\n"
        "    assert context.container_id == 'restored-container'\n"
        "    assert context.container_hostname == 'restored-host'\n"
    )
    monkeypatch.setattr("runner.handler_loading.USER_CODE_DIR", tmp_path)
    runner = FunctionRunner(
        FunctionRunnerConfig(
            stub_id="stub",
            handler_ref="restored_function:handler",
            container_id="before-checkpoint",
            container_hostname="before-checkpoint-host",
            checkpoint_enabled=True,
            lifecycle_hooks=LifecycleHooks(on_start=("restored_function:started",)),
        )
    )
    try:
        runner.run_startup_hooks_once()
    finally:
        runner.close()
