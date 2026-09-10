# Compute package

Provider-neutral capacity: offers, units, machines, private-agent state, managed
capacity lifecycle, billing hooks, and the protocols providers implement.

Provider adapters, scheduler and worker loops, the gateway, apps, and the SDK
stay outside; notify them through narrow hooks rather than importing them.
Durable access goes through services and repositories, hot state through the
coordination package.

- Capacity authority belongs here. A caller asks for capacity; it does not decide
  what exists.
- Preserve retries, billing attribution, and resource cleanup on every path that
  can create something billable. A leaked machine is a bug with a running meter.
- A credential never travels in machine configuration that provisioning can read
  back. Prefer a single-use credential vended at the moment of use over a
  reusable one embedded where every instance from that configuration can read it.
  Hetzner enrollment has one owner-approved exception: a unique, short-lived
  bootstrap token may travel in one node's user-data. Bind it to that launch,
  consume it atomically before tenant work starts, and reject reuse or expiry.
  Keep tokens out of source, images, logs, and shared launch templates. This does
  not permit a provider API key or reusable join credential on a node.
- A bootstrap script owns provider identity and nothing else. Everything it
  installs belongs to the artifact it downloads, so a version or a policy is
  pinned in one place instead of drifting between two.
- Distinguish a nonce from an authenticator. A value that grants nothing without a
  second, separately verified identity may be stored in the clear; a value that
  authenticates goes through the workspace secret cipher. Know what that cipher
  actually protects against before relying on it, and say so where it is used.

Platform capacity spans providers behind the pooled-capacity protocol. A stable
`provider_ref` resolves ownership, pool, limits, allowed offers, and lifecycle
policy. The AWS connection pointer belongs only to capacity backed by an actual
AWS connection. Platform bindings need no customer connection row or separate
purchase ceiling. Existing AWS connection limits sum across that connection's
units; account admission owns plan concurrency and billing limits.
Desired-capacity changes lock the
binding's capacity workspace before reading the sum or updating a unit.

Supplier quotes are immutable component estimates recorded when a node is first
observed. A unit records its prepared offer; reconciling its existing nodes must
not replace their terms with a later offer. These snapshots do not establish the
supplier's invoiced charge or identify an asynchronous launch's purchase quote.
Missing costs remain unknown, and
historical aggregates remain unallocated because today's catalog cannot establish
what a previous purchase included. CPU, RAM and GPU share one supplier compute
charge; the customer usage ledger is a separate billing boundary.

An authenticated machine enrollment owns its worker runtime. The worker's
identity and resource allocation derive from that enrollment, not from a
scheduler registration that can expire or stop accepting placement. Planned
draining preserves the runtime while its existing work finishes. Revoking a
machine or removing a slot is a lifecycle operation, not a consequence of a
placement status.

A connected cloud account's provisioning policy is defined in code. Customers
choose workload resources, not node types, warm floors, or acquisition limits.
Provider catalogs define the supported machine types. Removing a type must stop
new purchases while preserving observation, draining, and deletion of owned nodes.

Catalog review owns profitability assumptions. Provider catalogs approve regions,
instance types and purchase markets. Acquisition ranks suitable approved offers
by quoted compute, root disk and public IPv4 cost. Unknown component costs cannot
rank as free capacity. Do not add purchase ceilings, per-workload revenue
forecasts or a runtime margin engine. Owned nodes remain observable and removable
after their types leave the catalog.

A connected cloud account belongs to a user, not a workspace, and backs every
workspace that user owns. Runtime lookups therefore resolve
`workspace -> owner -> connection` through `get_for_workspace_owner`, and anything
that acts on the whole connection, such as draining its pools or reconciling its
bucket grants, gathers every workspace the owner holds first. Snapshotting one
workspace and applying it as the connection's whole state would revoke what the
others rely on.

A joined machine belongs to a user the same way. The join credential carries the
account, resolved at mint time from the owner of the minting workspace, and the
enrollment is stamped from that credential, never from anything the joining host
says about itself. From there the account travels outward: enrollment to hot agent
state to the scheduler's worker record, which is what placement compares. The
fingerprint is unique per account, so one physical host is one machine however many
workspaces its owner holds.

The machine's workspace is provenance, not tenancy. A unit has to live in a
workspace and so does the durable machine row, so the account's first workspace
anchors both; which one it is has no effect on who the machine serves. Transferring
that workspace to someone else therefore does not transfer the hardware, which is
the intended answer. The machine stays with the person who connected it.

Tenancy is stamped once, at the authority that decides it, and reconciled
afterwards rather than re-derived. `AgentWorkerPoolController` compares each live
worker's account against its machine's on every pass and narrowly updates the two
fields when they differ, so a record written before the machine had an account
converges instead of being refused by every placement forever. Reconciliation is
the reason stamping is safe; without it a stamp is a value that can only go stale.

Placement compares owners. A private worker is the customer's own machine and
serves every workspace that customer owns; accept the consequence deliberately,
that private capacity is no longer a hard isolation boundary between an owner's
own environments. It stops at the account. Platform-managed capacity is shared and
governed by a separate rule that no owner comparison may widen. Every gate on the
worker boundary, whether admission, credential vending, or network mutation,
compares the same pair, because a placement rule enforced in one of four places is
enforced nowhere.

A unit is deleted because someone deleted it. The provider cannot supply that
fact: an account with no autoscaling group in it is what a torn-down unit and one
that has never been built both look like, and the provider is asked about the
account. So a snapshot reporting nothing moves a unit to `Deleted` only when the
unit was already `Deleting` or `Deleted`, and otherwise means the unit is still
`Provisioning`. Every real teardown passes through `Deleting` first, so the gate
costs those paths nothing.

The asymmetry is the point. `reconcile_pooled_capacity` declines to build a
deleted unit, and `_prepare_pooled_capacity` is the only thing that revives one,
so a wrong `Deleted` ends the unit: the warm floor revives the row, the next
snapshot buries it, and the account holds a floor it can never fill while every
task dies naming a pool. A wrong `Provisioning` costs one pass. Prefer it.
