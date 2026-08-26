# Operator CLI app

`lazycloud-admin`, the internal operator CLI: administration, diagnostics,
local-service, and backend-backed commands that are not public SDK features.

It is a superset of the public `lazycloud` CLI, and shared public workflows are
reused from the public CLI package rather than reimplemented here. Commands stay
thin, machine-readable output stays stable and free of secrets, and a capability
that belongs on both CLIs lands on both in the same change.

`fleet ensure` and `fleet destroy` are a pair, and a deployment's life runs
between them. `ensure` registers the platform's own account so pools can be
provisioned in it; `destroy` deletes every unit so nothing is left to launch
machines once Terraform takes the cluster away. Neither reaches past the control
plane to the provider: a unit is the record that owns an autoscaling group, so
deleting the group without the unit leaves the scheduler free to build it again.
