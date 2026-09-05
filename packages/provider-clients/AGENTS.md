# Provider client composition

The mapping from persisted provider configuration and process settings to
concrete adapter instances.

This package owns construction and nothing else: not provider workflows, billing,
capacity reconciliation, API or CLI behavior, persistence tables, or process
entrypoints.

Deployment settings carry non-secret capacity policy and image catalogs; separate
secret settings carry credentials. Resolve bootstrap-created workspace identities
when capacity is used, not while constructing the services bootstrap itself needs.

Where that configuration comes from is part of the mapping, so this package also
owns the published release manifest contract and the resolution of a deployment's
settings from it. A deployment points at one release; the agent artifact version
and digest, the URL that serves it, the container-worker image, the customer
authorization template, and exact CPU and GPU AMI IDs are then read from that
release rather than authored beside it. Values a release cannot know, such as the
local artifact mount, instance prices, and control principal, stay
environment-owned, and a rule that spans both halves says which half is missing.
A deployment with no manifest resolves the same way to an absent release: that is
a control plane with no managed capacity, not a second mode.

The production registry admits only providers with live evidence behind them. A
persisted kind that is not supported fails explicitly and by name; it never
resolves to a stand-in that appears to work. Keep exports explicit, and let
credential and construction failures propagate rather than collapsing into a null
adapter that fails later somewhere less informative.
