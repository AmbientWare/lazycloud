from __future__ import annotations

from pathlib import Path

import pytest
from runner.hooks import run_lifecycle_hooks
from shared.lifecycle import LifecycleHookName, LifecycleHooks, LifecycleStartupContext


async def initialize(context: LifecycleStartupContext) -> None:
    if not context.workspace_id:
        raise ValueError("workspace is required")
    Path(context.handler).write_text(context.workspace_id)


def test_startup_hook_executes_with_context_and_propagates_failure(tmp_path: Path) -> None:
    hooks = LifecycleHooks(on_start=(f"{__file__}:initialize",))
    messages: list[str] = []

    def log(stream: str, message: str) -> None:
        messages.append(message)

    marker = tmp_path / "initialized"
    for capture in (False, True):
        context = LifecycleStartupContext(workspace_id=f"workspace-{capture}", handler=str(marker))
        run_lifecycle_hooks(
            hooks,
            LifecycleHookName.Start,
            context,
            log=log,
            capture_output=capture,
            raise_on_error=True,
        )
        assert marker.read_text() == context.workspace_id
    assert not messages
    with pytest.raises(ValueError, match="workspace is required"):
        run_lifecycle_hooks(
            hooks, LifecycleHookName.Start, LifecycleStartupContext(), log=log, raise_on_error=True
        )
