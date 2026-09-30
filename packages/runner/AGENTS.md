# Runner

- Execute inside user containers with shared contracts, small foundation loading
  helpers and user code only. No backend dependencies. Coordinate new execution
  contracts with the Rust host runtime; keep module entrypoints importable.
- Report guarded user imports as user failures. Ship the read-only content-addressed
  runner artifact with its worker image.
- Keep invocation identity/output in context variables, not process globals.
  Install stdout routing once; overlapping redirects are unsafe.
- Preserve process-slot isolation and shared-interpreter semantics. Failed
  `on_start` stops the container rather than serving without initialized state.
