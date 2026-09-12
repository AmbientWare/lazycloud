# Agent app

The entrypoint for the agent that runs on customer and private-unit machines:
arguments, settings, daemon startup, and outbound tunnel lifetime.

Reusable agent behavior belongs in `packages/agent`; this app only assembles it
and runs the process. Transport failures raise typed errors rather than
returning soft results. Environment variable names here are part of the install
and join flows, so they change together with the assets that set them.
