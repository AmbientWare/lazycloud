# Runner Package

The small transport-oriented entrypoints that execute inside user containers.

It runs beside arbitrary user code, so its dependency surface is deliberately
minimal: shared contracts and environment helpers plus the user's own code—never
backend domains, apps, persistence clients, schedulers, workers, or providers.
Calls use the current public HTTP contracts.

Keep the module entrypoints importable and guard user imports, so a failure in
user code is reported as a user failure with its own framing rather than as a
runner crash.

Runner ships as a versioned content-addressed artifact mounted read-only by the
worker. The artifact and the worker image that carries it move together.

## Serving more than one invocation at a time

A container serves many calls, and may serve several at once. Two shapes, and
the choice is the user's: a process per slot isolates them, and one interpreter
for all of them shares whatever `on_start` loaded — which is the only way a
model in VRAM is loaded once rather than per slot.

Nothing per-invocation may live in a process global. Task identity and output
attribution are held in `contextvars`, so each concurrent call answers for
itself and threads and coroutines both carry it. In particular `sys.stdout` is
installed once and routes per context: `contextlib.redirect_stdout` restores in
exit order rather than entry order, so two overlapping redirects leave the
stream pointing at a finished task's sink for every later caller.

`on_start` failing stops the container rather than failing one call. It loads
what the handler expects to be there, so a container that skipped it produces
failures blamed on the callers' code.
