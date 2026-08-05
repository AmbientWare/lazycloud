# Agent App

The entrypoint for the agent that runs on customer and private-pool machines:
arguments, settings, daemon startup, and route-proxy wiring.

Reusable agent behavior belongs in `packages/agent`; this app only assembles it
and runs the process. Transport failures surface as typed errors rather than
soft results. Environment variable names here are part of the install and join
flows, so they change together with the assets that set them.
