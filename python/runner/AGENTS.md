# Runner

- Execute inside user containers with `lazycloud._shared`, `lazycloud.contracts`
  and user code only. No backend dependencies. Serve the local runner protocol;
  transport, heartbeats, result transfer and draining belong to the host
  runtime. Keep module entrypoints importable.
- Report guarded user imports as user failures. The runner ships in the
  read-only managed runtime the agent mounts into each container.
- Keep invocation identity/output in context variables, not process globals.
  Install stdout routing once; overlapping redirects are unsafe.
- Preserve process-slot isolation and shared-interpreter semantics. Failed
  `on_start` stops the container rather than serving without initialized state.
