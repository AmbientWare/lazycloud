# Agent package

Reusable behavior for the agent that runs on customer and private-unit machines:
installation, daemon lifecycle, enrollment, and telemetry.

`worker_controller.py` owns Docker worker management and reserve records.
`state.py` owns the agent's local identity and readiness markers. A stream
observes worker containers once and passes those slots to reconciliation.

Process arguments and startup stay in `apps/agent`. API handlers, SDK code, app
imports, and broad composition stay out.

Reach dependencies through narrow protocols. A database context or gateway client
the agent needs is a protocol it declares, not a concrete implementation it
imports, because the agent runs where most of this repository is not installed.
