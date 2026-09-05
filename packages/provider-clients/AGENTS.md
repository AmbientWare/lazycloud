# Provider client composition

The mapping from persisted provider configuration and process settings to
concrete adapter instances.

This package owns construction and nothing else: not provider workflows, billing,
capacity reconciliation, API or CLI behavior, persistence tables, or process
entrypoints.

Where that configuration comes from is part of the mapping, so this package also
owns the published release manifest contract and the resolution of a deployment's
settings from it. A deployment explicitly pins three manifests. The control-plane
manifest supplies its release identity and customer authorization template. The
worker manifest supplies the container-worker image. The host manifest supplies
the agent executable and exact CPU and GPU AMIs. Routine Ship advances control
and worker pins while retaining the host pin; host upgrades are explicit.
Values a release cannot know, such as the
local artifact mount, instance prices, and control principal, stay
environment-owned, and a rule that spans both halves says which half is missing.
A deployment with no manifests has no managed capacity. Partial pin sets are
invalid. Terraform owns infrastructure identities, Helm owns these runtime
settings, and processes read environment variables rather than Terraform files.

The production registry admits only providers with live evidence behind them. A
persisted kind that is not supported fails explicitly and by name; it never
resolves to a stand-in that appears to work. Keep exports explicit, and let
credential and construction failures propagate rather than collapsing into a null
adapter that fails later somewhere less informative.
