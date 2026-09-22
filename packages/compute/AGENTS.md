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
`provider_ref` resolves ownership, placement, allowed offers, and lifecycle
policy. The AWS connection pointer belongs only to capacity backed by an actual
AWS connection. Platform bindings need no customer connection row. Account
admission owns plan concurrency and billing limits.

`FleetCapacityPolicy` owns platform CPU/GPU node limits and CPU warm minimums by
purchase market. Platform growth and planned replacement share a PostgreSQL
transaction lock before reading commitments or changing a unit. Terminating
nodes consume headroom until their absence is observed. Providers can replace
failed machines autonomously, so observed physical counts may briefly exceed
the platform's admitted commitments. Customer-owned capacity stays outside these
totals and serializes changes through its capacity workspace.

Warm reconciliation maintains a baseline of usable workers in each purchase
market. Workers serving requests count toward it. Healthy pools retain their
baseline; market changes transfer it one worker at a time. Retiring assets count
until the provider confirms their absence. Market changes, machine replacement
and worker updates share the platform maintenance lock. A zero minimum disables
that market's baseline. Floor changes preserve active work until it drains.
Customer capacity cannot satisfy the platform baseline.

CPU reserves use a separate stopped On-Demand target. Running targets, stopped
targets, preparation, and retiring assets share the CPU fleet budget. Placement
prefers compatible stopped capacity before buying another node. Queued CPU work
takes priority over reserve replenishment. A stopped target being removed keeps
its commitment until provider observation confirms its removal; disk destruction
still requires the existing provider evidence.

The agent proves its current binary and worker image before initial preparation
completes. Returning a used host requires a durable drain, no live workloads,
stopped worker processes, and a receipt fenced to the stop request and cache
generation. Cleanup removes tenant files and retires the worker credential while
preserving the agent identity and platform image cache. Resuming keeps the machine
identity and requires a new worker registration and request-poll lease.

CPU headroom grows after reserved CPU or RAM leaves at most 20 percent free for
60 seconds. Redis owns that observation window across scheduler replicas; pending
capacity prevents duplicate growth. The stopped target remains two. Keep the
running Spot floor at two until live preparation, restart, refill, interruption,
and cleanup acceptance passes; then lower it to one within the same four-node cap.

Within each purchase market, warm workers prefer distinct provider, region,
availability-zone and instance-type combinations. A provider risk report closes
new admission on its machine and records one durable recovery obligation.
Recovery buys compatible capacity outside that combination, within the existing
fleet cap. Multiple threatened machines may recover together. Planned updates
yield to recovery, and a source remains protected from elective retirement until
its replacement accepts requests. A real interruption deadline still stops work
on time. Advisory notices carry no deadline and do not stop active work.

PostgreSQL stores handoff sources, purchase demand IDs and fulfillment timestamps
outside JSON projections, so older writers preserve them. The database rejects
changes to a terminal operation's outcome or named machine, and rejects attempts
to restore its released ownership.

Supplier quotes are immutable component estimates recorded when a node is first
observed. A unit records its prepared offer; reconciling its existing nodes must
not replace their terms with a later offer. These snapshots do not establish the
supplier's invoiced charge or identify an asynchronous launch's purchase quote.
Missing costs remain unknown, and
historical aggregates remain unallocated because today's catalog cannot establish
what a previous purchase included. CPU, RAM and GPU share one supplier compute
charge; the customer usage ledger is a separate billing boundary.

A machine has one lifecycle, `shared.compute_fleet.MachineLifecycle`, stored on
its row with the message, the failure reason and the time it entered the phase.
`compute.machine_lifecycle.advance_machine_lifecycle` is the only place a phase
moves, and it refuses a move backwards: `failed` and `deleted` are reachable
from anywhere, a fresh join is the one way back into `joining`, and re-stating
the current phase keeps `lifecycle_at` so a reclaim clock is not restarted by a
heartbeat. `write_machine_lifecycle` persists the move and publishes
`compute.machines` after the session commits; nothing writes the phase around
it. The row exists from the first time the platform hears of a machine: the
join command mints a joined host at `requested`, and the provider reconcile
creates a launched node's row the first time the provider reports the instance,
binding the provider row to it, so a provider row with a `machine_id` does not
mean the node enrolled; `first_enrolled_at` on the provider row says that.
Reclaim deadlines run from `lifecycle_at`; silence is not a phase, so a machine
that stops reporting stays `ready` with a message, and the enrollment's
readiness plus the worker record decide whether it serves. The machine's
`ResourceStatus` is kept in step by the writer for the repository queries that
filter on it and is not exposed.

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

Provider catalogs approve regions, instance types and purchase markets. Compute
owns purchase admission, using the effective shared rate card and the fleet
margin policy. Revenue includes sellable CPU, requested RAM after reservation
headroom, and GPUs. It describes a fully packed node, not realized profit. Use
the applicable customer placement rate without assuming a location premium or
charging Spot-tolerant work the non-preemptible premium.

Compare revenue with the complete compute, root disk and public IPv4 quote.
Unknown prices refuse platform purchases. Apply the same decision to selection,
warm capacity, growth and platform-controlled restoration. Customer-owned
infrastructure stays outside the platform margin policy. Existing nodes remain
observable and drainable after their offer fails purchase admission. Purchase
permission belongs to the code-defined provider policy. Disabled platform providers
remain resolvable for cleanup but cannot grow or receive warm targets. Their
provider-managed launch processes must also stop; in-flight launches still settle
through enrollment and reconciliation. The margin assessment does not cap future
Spot prices or autonomous replacement prices on enabled providers.

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

A joined machine has a name, unique within its owner's account, and an explicit
list of the workspaces it serves. Both live on the machine row and its
`machine_workspaces` links, written when the join command is minted, so the name
is taken before the host ever connects and the database refuses a second live
machine with it. Each machine is its own unit, placed on the machine's id; the
placement is the only thing that selects it, and the name is a lookup key. The
first listed workspace anchors the unit and the machine row, which is provenance,
not tenancy: the served list is what says who may run there, and changing it
moves nothing. A workspace with a deployment still pinned to the machine cannot
be dropped from that list.

Placement is an identity, `shared.placement.Placement`: the platform, one
connected account by connection id, or one joined machine by machine id. It has
no policy. `resolve_placement` answers with the workspace's location, the
platform or the connected account named by the workspace's `connection_id`,
unless the workload names a machine, in which case it answers with that
machine's placement or refuses with the name in the error. There is no default
to change and no fallback in either direction; a workload that names a machine
runs there or nowhere. A deployment pins the placement and the name it resolved
to at deploy time, so a workspace whose location later changes moves nothing
already running. A stub carries no placement. Runs, sandboxes and shells reuse
a stub across many starts, and `execution.placement.workload_placement` resolves
the workspace and the named machine again for every container request; a
machine that has left fails the request at once with its name in the error.

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

Deletion requires durable intent. Missing provider resources move a unit to
`Deleted` only when it was already `Deleting` or `Deleted`; an unbuilt unit stays
`Provisioning`. Preparing capacity can revive a fully `Deleted` unit with a new
generation. A `Deleting` unit cannot reactivate while provider teardown is in flight.

Empty internal platform units retire when they hold no capacity, warm floor,
active work, reservations, or pending replacement.
New units retain their idle-drain grace period unless their binding is obsolete.
Retirement holds the reservation mutation and dispatch leases, checks durable
demand and provider storage destruction, and preserves the unit and machine
history. Preparing a candidate and reserving its capacity share the same mutation
lease. A later purchase revives the same identity with a new generation.
Customer-owned pools do not expire through this policy.

Retired pools retain failure history. A failed market becomes eligible again
after its cooldown, once previous operations and provider cleanup finish.
Only new demand or a baseline selection can restore its desired capacity.

Storage destruction evidence carries the time the provider confirmed absence,
not the reconciliation pass start time. A cache generation may register while
that provider call runs; generation ownership and evidence chronology must both
hold before its storage can be retired.
