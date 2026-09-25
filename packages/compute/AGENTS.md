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
  back. A node proves who it is with signed instance identity, so user-data,
  images and launch templates carry no token, provider API key or join
  credential. Prefer a single-use credential vended at the moment of use over a
  reusable one embedded where every instance from that configuration can read it.
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

`FleetCapacityPolicy` owns the platform's node limits, its running vCPU cap and
each market's headroom. Platform growth and planned replacement share a
PostgreSQL transaction lock before reading commitments or changing a unit.
Terminating nodes consume headroom until their absence is observed. Providers can
replace failed machines autonomously, so observed physical counts may briefly
exceed the platform's admitted commitments. Customer-owned capacity stays outside
these totals and serializes changes through its capacity workspace.

Platform reserves are headroom, not machine counts. A market is the purchase
market work accepted, Spot or On-Demand, and for GPUs the card. Each keeps running
headroom and stopped headroom in CPU, memory and cards: a floor, a share of the
market's reserved load, and a ceiling. The Spot stopped target also covers the
busiest Spot machine's load, so an interruption resumes onto reserves. Defaults:
Spot keeps 12 vCPU and 24 GiB running (two small machines) and 28 vCPU and
100 GiB stopped (one large); On-Demand keeps nothing running and one small and one
large stopped; T4, A10G and L4 keep one stopped On-Demand card, larger cards none.
Headroom is measured in schedulable capacity, what a node gives containers. A
node's memory is the least its shape's enrolled machines reported, kept once per
nominal CPU, memory and card count in `compute_node_shapes`, and the offer's
nominal size until one enrolls.

`plan_market_reserve` is pure and runs on one scheduler replica a minute, or
sooner when a market's running headroom stays short for a minute. It reads one
snapshot statement. Growth resumes a stopped machine in the market before it buys
one: the smallest while the market's load is within its running floor, the largest
above it, and a purchase follows the same rule with small and large machines.
Idle machines beyond the running target are released largest first when quiet.
Stopped reserves are prepared, one per market per pass, while demand waits for
none; a large one when the shortfall exceeds a small machine. Reserves above the
target retire. A market with waiting work or an interruption recovery takes no
reserve growth, so a request's purchase is never behind a reserve's. A resume
lowers the unit's stopped count, so refilling a reserve is always the planner's
decision. Spot-tolerant work may resume an On-Demand reserve only while the
On-Demand reserves left still meet their floor; otherwise it buys a new machine
in that unit and the reserve stays stopped, because a retained pool resumes a
reserve only when its running and stopped counts leave no room to launch. A slot
being retired is not counted, so it neither delays a launch nor forces a resume.

A unit's `min_machines` is the planner's count of serving machines the idle drain
keeps, and the drain keeps its busy machines first. Running, stopped, preparing and
retiring machines share the fleet budget; stopped machines do not count against the
running vCPU cap. A stopped target being removed keeps its commitment until
provider observation confirms its removal; disk destruction still requires the
existing provider evidence.

Platform AWS pools, CPU and GPU, own EC2 instances directly. Spot launches use
persistent requests with stop interruption behavior. Cleanup cancels each request
before terminating its instance and holds capacity until its disks are gone.
PostgreSQL checkpoints record launch intent before provider mutations. Their
revision fences concurrent writers and stale inventory responses. Existing
Auto Scaling groups retain their recorded resource owner through cleanup.

The agent proves its current binary and worker image before initial preparation
completes, and the provider row records them. A GPU reserve also proves its image
and driver: its enrollment reports the unit's cards through the driver and passes
preflight before it stops. A stopped reserve that predates the active release is
started and prepared again, one machine at a time and only while no platform work
waits, so a resume never updates itself first. The scheduler looks for such
reserves within one capacity pass of a release activating, then each minute. A
refused reserve launch or an interrupted reserve leaves the pool serving. Until
the failure's cooldown passes, that unit keeps the reserves it holds and buys no
more; the shortfall goes to another unit. Finishing a preparation needs the pool's
lease, and a stream that finds it held defers that step instead of failing.
Returning a used host requires a durable drain, no live workloads, stopped worker
processes, and a receipt fenced to the stop request and cache generation. A used
host becomes a reserve only while its market's reserves fall short of the last
plan's stopped target. Cleanup removes tenant files and retires the worker
credential while preserving the agent identity and platform image cache. Resuming
keeps the machine identity and requires a new worker registration and
request-poll lease.

A reserve prepared without serving keeps its worker slot and credential, and its
agent records them. A retained machine of a type EC2 can hibernate, with under
150 GiB of RAM, launches with hibernation configured and a root volume grown by
its RAM. Its offer lists the usable disk and prices the whole volume. Such a
reserve runs its worker while it is prepared. Compute asks the provider to
hibernate it only once that worker runs the release and the agent reports it
waiting at its first call, so it resumes with both running. Any other reserve,
a used machine returning to reserve included, stops plainly, and its agent
starts the recorded worker at the next boot. EC2 refuses to hibernate a guest
that is not ready yet, so a refusal is retried each pass and becomes a plain
stop after ten minutes, or at once for an instance launched without
hibernation. A stop of either kind still pending ten minutes after its request
is forced.

Until a stream adopts that worker, the agent keeps its control listeners closed.
The agent starts it with `WORKER_ADMISSION_HOLD_SECONDS`, so the worker retries
its first call for up to 30 minutes of awake time and then fails; every other
worker gives up after a minute. The control plane holds the same fence: no
worker registers on a machine whose provider row is `preparing`, `stopping` or
`stopped`, or which is `stopping`, `stopped` or `resuming` before its provider
row is active. While that row is not active, only the stream's resume
authorization moves a resumed reserve to `joining`, and the agent's own joining
report leaves the machine alone. Once the provider reports the instance active,
its report or the next capacity pass moves a machine still in a reserve phase to
`joining`. A used host retires its credential, so it resumes through the stream.
An agent that booted after its reserve was prepared says so on each stream. A
row still reading `stopping` or `stopped` then lags the resume, so the worker
waits, fenced, as a hibernating reserve's does, rather than being stopped.

A provider lists its regions in the order platform purchases prefer them. When
offers in two combinations of one region refuse launches within 30 minutes, that
region ranks after the others, so a shortfall moves to the next region rather than
walking the sold-out one type by type. Within a region, reserve purchases prefer
zones the market does not run in yet. A provider interruption notice closes new
admission on its machine and records one durable recovery obligation. Recovery buys
compatible capacity outside that combination, within the existing fleet cap, and
platform capacity may recover in another region. Multiple threatened machines may
recover together. Planned updates yield to recovery, and a source remains
protected from elective retirement and consolidation until its replacement
accepts requests. The interruption deadline still stops work on time. Rebalance
advisories are not read: in a scarce Spot market EC2 posts one at boot, and acting
on it retired every Spot machine before it served.

PostgreSQL stores purchase demand IDs and fulfillment timestamps outside JSON
projections, so older writers preserve them. The database rejects changes to a
terminal operation's outcome or named machine, and rejects attempts to restore its
released ownership.

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
choose workload resources, not node types, reserves, or acquisition limits.
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
reserves, growth and platform-controlled restoration. Customer-owned
infrastructure stays outside the platform margin policy. Existing nodes remain
observable and drainable after their offer fails purchase admission. Purchase
permission belongs to the code-defined provider policy. Disabled platform providers
remain resolvable for cleanup but cannot grow or hold reserves. Their
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

Empty internal platform units retire when they hold no capacity, retained
machines, active work, reservations, or pending replacement.
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
