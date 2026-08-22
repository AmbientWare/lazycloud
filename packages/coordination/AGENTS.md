# Coordination package

Backend-neutral Redis clients and settings, key and serialization helpers, and
the hot state, events, locks, leases, and pub/sub built on them.

Domain Redis repositories and the workflow decisions that use them stay with
their owners. Apps, database, providers, and product services stay out.

The point of this package is that atomicity, lease semantics, serialization, and
reconnect behavior are decided once and correctly. Keep those decisions explicit
and keep them here, rather than letting each consumer reimplement a slightly
different version of the same guarantee.
