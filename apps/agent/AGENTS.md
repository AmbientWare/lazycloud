# Agent app

The entrypoint for the agent that runs on customer and private-unit machines:
arguments, settings, daemon startup, and outbound tunnel lifetime.

Reusable agent behavior belongs in `packages/agent`; this app only assembles it
and runs the process. Transport failures raise typed errors rather than
returning soft results. Environment variable names here are part of the install
and join flows, so they change together with the assets that set them.

The daemon's main thread owns the machine's workers. It starts a reserve's
worker after the tunnel connects and before the first stream, and every stream
applies and stops workers on the same thread; new worker work joins it rather
than starting another thread. The capacity shutdown timer runs beside it and
never waits for it: shutdown halts the worker controller, which then starts no
worker, and a start already under way removes its own container. A lock covers
only the slot file. Every Docker command waits for the daemon, up to a minute
from agent start, because the agent starts before Docker. Waits between streams
block on the agent's wakeup, which a finished image or a resume from sleep ends,
instead of polling.
