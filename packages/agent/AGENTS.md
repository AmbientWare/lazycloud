# Agent package

Reusable behavior for the agent that runs on customer and private-unit machines:
installation, daemon lifecycle, enrollment, and telemetry.

Process arguments and startup stay in `apps/agent`. API handlers, SDK code, app
imports, and broad composition stay out.

Reach dependencies through narrow protocols. A database context or gateway client
the agent needs is a protocol it declares, not a concrete implementation it
imports, because the agent runs where most of this repository is not installed.
