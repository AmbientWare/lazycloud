"""Lifecycle hooks: user callables the runner calls around each attempt.

Each hook is a `module:qualname` reference into the source, called with one
context object. on_start runs once per runner process after the handler
loads, and its failure is a load failure. The others run in order around an
attempt; a failing one is reported on stderr and the rest still run.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from collections.abc import Callable
from typing import Any

from shared.lifecycle import (
    LifecycleHookName,
    LifecycleHooks,
    LifecycleStartupContext,
    LifecycleTaskContext,
)

from runner.handler_loading import load_handler
from runner.protocol_models import LifecycleHooks as HookFrame


def hooks_from_frame(frame: HookFrame | None) -> LifecycleHooks:
    if frame is None:
        return LifecycleHooks()
    return LifecycleHooks.model_validate(frame.model_dump(exclude_none=True))


def _call(reference: str, context: object) -> None:
    hook: Callable[..., Any] = load_handler(reference)
    target = getattr(hook, "func", hook)
    result = target(context)
    if inspect.isawaitable(result):
        asyncio.run(_await(result))


async def _await(value: Any) -> Any:
    return await value


def startup_context(handler: str) -> LifecycleStartupContext:
    return LifecycleStartupContext(
        workspace_name=os.environ.get("LAZYCLOUD_WORKSPACE", ""),
        container_id=os.environ.get("CONTAINER_ID", ""),
        container_hostname=os.environ.get("HOSTNAME", ""),
        handler=handler,
    )


def run_startup_hooks(hooks: LifecycleHooks, handler: str) -> None:
    """Run on_start; the first failure propagates and fails the load."""

    context = startup_context(handler)
    for reference in hooks.on_start:
        _call(reference, context)


def run_task_hooks(
    hooks: LifecycleHooks, hook: LifecycleHookName, context: LifecycleTaskContext
) -> None:
    """Run one task hook's callables; failures go to the attempt's stderr."""

    context = context.model_copy(update={"hook": hook})
    for reference in hooks.refs(hook):
        try:
            _call(reference, context)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            print(
                f"lifecycle hook failed: {reference}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )


__all__ = ["hooks_from_frame", "run_startup_hooks", "run_task_hooks", "startup_context"]
