from __future__ import annotations

from typing import Protocol

from foundation.handler_loading import load_callable
from shared.lifecycle import (
    LifecycleHookName,
    LifecycleHooks,
    LifecycleStartupContext,
    LifecycleTaskContext,
)

from runner.invocation import invoke_handler
from runner.runtime import routed_output


class HookLogger(Protocol):
    def __call__(self, stream: str, message: str) -> None: ...


LifecycleContext = LifecycleStartupContext | LifecycleTaskContext


def lifecycle_hooks_from_env(raw: str | None) -> LifecycleHooks:
    if not raw:
        return LifecycleHooks()
    return LifecycleHooks.model_validate_json(raw)


def run_lifecycle_hooks(
    hooks: LifecycleHooks,
    hook: LifecycleHookName,
    context: LifecycleContext,
    *,
    log: HookLogger,
    capture_output: bool = True,
    raise_on_error: bool = False,
) -> None:
    """Run every hook registered for this point in the lifecycle.

    A failing hook is normally logged and stepped over: it is commentary on work
    that happened, and losing the commentary must not lose the work.

    `raise_on_error` is for the one hook where that is wrong. A container whose
    `on_start` did not finish has not loaded whatever the handler expects to be
    there, so serving calls with it produces failures blamed on the callers'
    code. Reported and swallowed, the whole container's worth of invocations
    fails one at a time for a reason that appears nowhere near the cause.
    """

    for reference in hooks.refs(hook):
        try:
            callback = load_callable(reference)
            if not capture_output:
                invoke_handler(callback, (context,), {})
                continue
            stdout = _HookLogStream("stdout", log)
            stderr = _HookLogStream("stderr", log)
            # Routed per context rather than swapped process-wide: task hooks
            # run inside an invocation, and several invocations run at once.
            with routed_output(stdout, stderr):
                invoke_handler(callback, (context,), {})
            stdout.flush()
            stderr.flush()
        except BaseException as exc:
            log("stderr", _hook_error(reference, exc))
            if raise_on_error:
                raise


class _HookLogStream:
    def __init__(self, stream: str, log: HookLogger) -> None:
        self.stream = stream
        self.log = log
        self._buffer = ""

    def write(self, value: str) -> int:
        if not value:
            return 0
        self._buffer += value
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self.log(self.stream, f"{line}\n")
        return len(value)

    def flush(self) -> None:
        if self._buffer:
            self.log(self.stream, self._buffer)
            self._buffer = ""

    def isatty(self) -> bool:
        return False


def _hook_error(reference: str, exc: BaseException) -> str:
    return f"lifecycle hook failed: {reference}: {type(exc).__name__}: {exc}\n"


__all__ = [
    "HookLogger",
    "LifecycleContext",
    "lifecycle_hooks_from_env",
    "run_lifecycle_hooks",
]
