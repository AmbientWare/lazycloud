# Operator CLI App

`lazycloud-admin`, the internal operator CLI: administration, diagnostics,
local-service, and backend-backed commands that are not public SDK features.

It is a superset of the public `lazycloud` CLI, and shared public workflows are
reused from the public CLI package rather than reimplemented here. Commands stay
thin, machine-readable output stays stable and free of secrets, and a capability
that belongs on both surfaces lands on both in the same change.
