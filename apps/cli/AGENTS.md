# Operator CLI app

`lazycloud-admin`, the internal operator CLI: administration, diagnostics,
local-service, and backend-backed commands that are not public SDK features.

It is a superset of the public `lazycloud` CLI, and shared public workflows are
reused from the public CLI package rather than reimplemented here. Commands stay
thin, machine-readable output stays stable and free of secrets, and a capability
that belongs on both CLIs lands on both in the same change.

`platform initialize` creates the application namespace and validates configured
provider bindings through offline deployment authority. It needs no account token.
`fleet destroy --confirm-stopped` uses the same deployment authority to remove
only platform units through the compute service. Stop admission and schedulers
first. Customer capacity stays outside this command's scope. Provider resources
retain their owning unit until the provider confirms cleanup.

Joining a machine is a public workflow and lives in the public CLI. The operator
CLI keeps unit create, list, and scale for platform capacity; its `--pool` on
`unit create` is the internal capacity label, not something a workload chooses.
