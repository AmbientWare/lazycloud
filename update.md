# LazyCloud architecture and implementation guide

## Purpose

Rebuild the backend and host runtime in Go, with the current application as the
1:1 product example. The rewrite is the same product done better. Every
capability, workflow, SDK and CLI command, dashboard feature and documented
behavior of the reference exists in the rewrite and works the same way for
users. The implementation is simpler, faster and more reliable. Keep the Python
SDK, public CLI and Python runner, and the TypeScript frontend as language
choices; their implementations can change. Make ownership clearer, remove
unnecessary work and reduce the code and operational machinery needed for
equivalent capabilities.

This document defines the target architecture and the prompt for an agent assigned
one area. The checklist tracks separate capabilities, not an implementation order.
Detailed task plans, findings and acceptance evidence belong with each task.
AGENTS.md requires agents to read and follow this document.

Correctness, simplicity and performance are separate requirements. A language
change, smaller source tree or passing tests alone does not establish success.

## Reference and branch state

- Branch: go-rewrite, created from local main.
- Pinned baseline: 9e259ce7545d620435dfaa572e63631834c7401e.
- Main reference checkout: /home/cmclean/.t3/worktrees/lazycloud/t3code-9782b237.
- Rewrite checkout: /home/cmclean/.t3/worktrees/lazycloud/go-rewrite.

Treat the reference checkout as read-only. Its branch may advance; the pinned
commit defines this baseline. Use git worktree list to locate main on another
machine, and git show <baseline>:<path> when comparing exact behavior. Do not rely
on the unrelated checkout at /home/cmclean/programs/ambient/lazycloud being main.

Use reference code, public docs, consumers and acceptance scenarios to discover
supported behavior and guarantees. Trace real workflows rather than translating
classes. Record defects separately. Existing package names, service topology,
queries, locks, intermediate states and algorithms are not requirements.
Later fixes on main need explicit evaluation; do not silently mix baselines.

The initial tree retains the Python client, runner, shared contracts, frontend
and public docs. Old backend code, database migrations, Go host helpers, admin
CLI, deployment automation and backend-specific tests remain in the reference and
Git history. The backend is not implemented here yet. Removing its files is
preparation, not a completed capability or a performance improvement. Existing
client tests are not replacement-backend acceptance.

The reference Go helpers (apps/disk-engine, apps/sandbox-supervisor and
apps/image-runtime) already run on hosts. Evaluate them against the host runtime
design and bring over what fits; they are evidence, not code to copy unchanged.

## Same product, better implementation

The reference is the specification of what users get, one to one. Its public
docs, SDK API, CLI commands and flags, dashboard pages, deployed endpoint URLs and
error behavior define the target. An area is done when it matches the reference
for users and beats it on correctness, simplicity or performance. A missing
command, option or page is a parity gap, never a simplification. Record every
intentional user-visible difference with its reason in the area's task file.
Differences are limited to fixing defects and making a behavior clearer or
faster.

Internals are free. Backward compatibility with the Python backend's internal
interfaces is not required. Design new management APIs, host protocols,
schemas, configuration and deployment topology where they make the platform
simpler, and update SDK, CLI, runner and web consumers together so users see
the same product. Do not build adapters, dual schemas, old endpoints or data
migration bridges just to support the old implementation.

Preserve required user outcomes, isolation, durability and recovery. Existing
package names, service topology, queries, locks and algorithms are evidence of
what the product needs, not structure to copy.

New local installations use a fresh PostgreSQL schema and native SQL migrations.
Old revisions remain immutable in the pinned reference. Importing old production
data, resetting production or replacing a live environment is a separate task
requiring explicit scope and authorization.

## Languages, layout and naming

- Go owns platform backend decisions, orchestration and host runtime.
- Python owns deployment authoring, SDK/CLI workflows and execution of Python user
  code. The public CLI is in python/lazycloud. The former apps/cli was the internal
  admin CLI; rebuild its supported operations through administration contracts.
- TypeScript owns the existing frontend. Future TypeScript and Go SDKs should use
  the same public protocol without depending on Python implementation details.
- The Python shared package supports retained consumers. Reduce it as contracts
  and consumers change; never turn it into a second backend.

Use ordinary domain names such as control, execution, scheduler, compute, agent
and storage. Do not add Go, New, Next, V2 or rewrite prefixes/suffixes to product
types, modules, binaries or configuration. Preserve genuine public API versions.

Repository layout:

- `go.mod` at the root: one Go module, added with the first implemented workflow.
- `cmd/server`, `cmd/scheduler`, `cmd/agent`: binaries that own composition and
  process lifetime.
- `internal/<owner>`: one package per owner, such as `internal/execution`. Split
  a package only for a concrete dependency, security or reuse boundary.
- `migrations/`: the single ordered SQL migration chain.
- `contracts/`: language-neutral OpenAPI, Protobuf and contract cases. Go, Python
  and TypeScript bindings are generated from here.
- `python/`: SDK/CLI, runner and shared Python contracts. `web/`: the frontend.

Host runtime packages must not import persistence or backend owner packages. An
import lint enforces this. Do not pre-create empty packages for this ownership
map. External container, networking and image tools can remain external
dependencies with explicit contracts.

## Ownership

| Owner | Decisions and state it owns | Boundary |
| --- | --- | --- |
| Control | Apps, immutable workload definitions, deployments, release selection, schedules, desired active/paused/deleted configuration | Requests execution changes; does not mutate task or container internals |
| Execution | Admission, queued/in-flight work, attempts, retries, results, cancellation, container lifecycle, endpoint concurrency/draining and desired workload replicas | Requests container placement; owns enforcement of execution invariants |
| Scheduling | Matching pending container requests to eligible workers, placement fairness and assignment reservations | Uses execution requests and compute capacity; does not decide task retry or provider lifecycle |
| Compute | Machines, worker enrollment/readiness, pool capacity, provisioning, provider interruptions, retirement and host release eligibility | Supplies capacity and withdrawal signals; does not own workload task states |
| Identity | Users, membership, authentication, scoped credentials and authorization policy | Every entry point invokes the same policy; domain checks protect resource ownership |
| Billing | Prices, grants, balances, payment/provider operations, ledger and spend admission | Supplies explicit admission decisions and records authoritative charges |
| Usage | Metering observations and attribution of actual resource use | Publishes idempotent usage facts to billing; no parallel balance authority |
| Storage | Objects, uploads, artifacts, volumes, disk attachments, snapshots and retention policy | Owns byte lifecycle and attachment authority; delegates host I/O through commands |
| Images | Image/build identity, deduplication, build attempts and artifact publication | Uses execution capacity; does not implement another scheduler |
| Networking | Routes, tunnel ownership, domain bindings and connection lifecycle | Transport forwards bytes; execution authorizes dispatch and compute authorizes hosts |
| Observability | Delivery/query of events, logs and metrics | Observes committed outcomes; does not become a second workflow engine |
| Notifications | Delivery preferences, destinations and retryable notifications | Consumes owner outcomes; cannot determine execution success |
| Host runtime | Local containers, processes, mounts, networking, source/image caches and supervision | Reports observed state under assignment authority; no backend database access |
| SDK/CLI/runner/web | Their respective user workflows and presentation | Consume public contracts; do not reproduce backend policy |

These are logical owners, not required packages or deployed services. Closely
related owners can share a package. Storage policy and host disk I/O are distinct
responsibilities, as are host identity verification and local identity proof.

Execution decides how many containers a workload needs. Scheduling decides where
those containers run. Compute decides how to obtain and retire machine capacity.
A scheduling binary may host all three loops, but it must not merge their decisions.

Cron owns when an occurrence is due and its occurrence identity. It submits through
normal execution admission. Maps fan out through normal task admission. Queues,
pods, endpoints and functions retain distinct supported semantics while sharing
the actual lifecycle operations they have in common.

Secret storage and access belong with identity/storage boundaries; execution
receives authorized references and delivers narrowly scoped material to workloads.
Shell/SSH sessions belong to execution and use networking transport. They must not
create another container lifecycle.

## Processes and dependency direction

Begin with the smallest deployable set justified by workloads:

- Server: public/admin APIs and necessary ingress, composed from domain owners.
- Scheduler: durable due-work processing, scaling, placement and fleet loops.
- Agent: machine connection and local runtime supervision.
- Python runner: user execution inside its container over the local runner protocol.
- Web: the existing frontend.

Separate ingress, connection gateways or worker processes when independent scaling,
trust, failure isolation or lifetime requires it. Existing process splits are
evidence to examine, not an obligation to preserve. Do not put a network call
between modules merely because their owners differ.

Handlers call owners; owners use persistence and explicit provider interfaces.
Repositories do not call services. Providers implement the owning domain's narrow
protocol. Binaries construct dependencies and own startup/shutdown. Avoid circular
callbacks and service bundles that grant each owner access to the entire app.

Cross-owner operations use an explicit transaction coordinator when their facts
must commit together. Do not distribute one required PostgreSQL transaction across
RPC calls. Later external effects use durable intent and idempotent delivery where
necessary. Reuse actual recovery mechanisms rather than adding a universal bus,
outbox or workflow framework to every operation.

## Communication and implementation choices

These are the starting choices. A task may propose a change with a concrete
simplification, operational or performance reason before introducing a second path.

| Boundary | Choice | Reason |
| --- | --- | --- |
| Modules in one process | Direct Go calls with dependencies passed explicitly from main | No serialization, RPC or remote repository façade |
| Local asynchronous work | Goroutines owned by an errgroup, cancelled through context, with bounded channels where ownership requires a queue | Backpressure and cancellation without a broker for in-process work |
| Public control API | net/http JSON from an OpenAPI spec in contracts/, with generated Go server types | Browser and multi-language SDK access through resource operations |
| Control plane to host | grpc-go/Protobuf with an authenticated outbound bidirectional host session | Typed commands, observations and resumable delivery over a real network boundary |
| User HTTP/WebSocket traffic | Streaming HTTP/WebSocket forwarding | Preserve streaming and avoid buffering bodies or wrapping every chunk in control messages |
| Database | pgx with sqlc-generated queries and plain SQL migrations | Typed results, visible query cost and explicit transactions |
| Artifacts and bulk bytes | Authorized object-store transfers and host caches | Keep large payloads off control streams |
| Telemetry | slog and OpenTelemetry with task/request/assignment correlation | Observe the same workflow across processes without owning its state |

The OpenAPI spec is the source for the public API. Go server types and the Python
and TypeScript client models are generated from it. grpc-go supports bidirectional
streaming with generated Protobuf bindings. sqlc generates typed Go from SQL, and
pgx gives explicit transactions, batching and COPY. These capabilities support the
choices; they do not establish LazyCloud performance without measurements.

The host session carries semantic commands such as start, stop, drain and report
exit. Include command identity, assignment generation, deadline and acknowledgement
semantics. A transport acknowledgement is not durable completion. After reconnect,
reconcile outstanding commands and observed state against durable authority.
Bound queues and retained replay; handle duplicate delivery and reordered reports.

Control messages and workload data have distinct flow control and limits. Slow log
consumers or large responses must not delay leases, stop commands or heartbeats.
Private hosts may still require an outbound tunnel for data; its design must prove
backpressure, cancellation and routing isolation. The control stream is not a
generic tunnel for all bytes.

Do not introduce a remote repository API, universal event bus, actor framework,
distributed transaction manager or dependency-injection framework by default.
Use an owner loop with a bounded mailbox only where serial ownership simplifies a
real local invariant. Keep stateless decisions as ordinary functions.

Use native async I/O throughout request paths. Put blocking container/filesystem
operations and CPU-heavy work behind bounded execution. Persist accepted work
before acknowledging it; an in-memory channel is not a durable queue.
Redis is optional per mechanism, not mandatory middleware for every request.

## Parallel agent development

Several agents can work concurrently after the shared boundary decisions are
concrete. Logical ownership enables parallel work; it does not justify adding
microservices or wrappers just to give each agent a directory.

An integrating agent first establishes the first slice's command/event shapes,
resource identifiers, error semantics, ownership, transaction boundaries, workspace
layout and test acceptance. This is a small concrete contract, not a platform-wide
framework. Land required shared definitions before dependent agents implement them.

Assign bounded work packets. Each states the public outcome, owned files/modules,
dependencies, contracts consumed/provided, invariants, acceptance and deletions.
Use a separate task branch/worktree based on go-rewrite per concurrent agent.
The reference remains read-only; never use its credentials or services implicitly.

| Work stream | Owns | Required agreement before parallel implementation |
| --- | --- | --- |
| Execution | Admission, task/attempt/container state and workload demand | Identity authorization result, execution request and assignment contract |
| Scheduling and compute | Placement, capacity snapshots, host enrollment and provisioning | Execution demand, capacity eligibility and fenced assignment |
| Host runtime | Agent session, container supervision, observations and local cleanup | Host command/event protocol, credential scope and artifact access |
| Clients and transport | Public API composition, Python SDK/CLI/runner and web integration | Resource schemas, typed errors, streams and authentication |
| Storage and images | Artifact/build/disk workflows and provider adapters | Execution request and authorized storage/attachment contracts |

Start with two or three owner agents plus the integrator. Increase concurrency only
when work packets are independent. Storage, billing, identity and other areas can
run as separate packets when their consumers and contracts are ready. This table
defines possible streams, not a demand to implement everything simultaneously.

The integrator owns root workspace/toolchain files, lockfiles, shared protocol
definitions, migration ordering and this architecture document. Domain agents
propose changes to those shared files; they do not edit them concurrently. Each
owner owns its schema design; the integrator coordinates shared invariants and
migration ordering. Schema changes spanning owners are one coordinated task.

Agents must not implement the same lifecycle in different streams, add a fallback
because a peer is unfinished, or mark integration complete against test doubles.
Missing dependencies are explicit gaps. Use focused owner checks during development,
then integrate a complete workflow against real services before accepting it.

Review returned diffs and rerun integrated acceptance. Resolve contract drift at its
source. Each completed packet reports capabilities, deleted machinery, measurements
and limitations. Checklist completion is the integrator's verified decision.

## State and recovery

PostgreSQL is authoritative for desired configuration, accepted tasks and attempts,
durable scheduling intent/assignment, occurrence identity, resource ownership,
usage/ledger records and effects that must survive a restart.

Redis holds transient leases, wake signals, connection presence and performance
projections. A Redis projection must name its durable source and recovery path.
Do not make Redis and PostgreSQL independently authoritative for container status.
Object stores/filesystems hold bytes; the database holds ownership and metadata.

For each workflow specify:

- Its durable acceptance point and the transaction invariant.
- Which owner may advance its state and which token/generation fences stale work.
- What happens if the process dies before and after every external side effect.
- Which retries are safe, how duplication is recognized and when work becomes due.
- How cancellation, draining and cleanup converge without losing accepted work.

Keep states only when they affect an externally meaningful decision or recovery.
A state machine shared by all resource kinds is not a goal. Endpoint request
leases, function attempts and long-lived pod lifetimes have different semantics.

Claim work only when it can execute. Bound concurrency, renew active leases when
needed, fence late completion and isolate per-item failures. Persistent failure of
one due item must not starve later work. Retain final atomic admission, quota,
assignment and retirement checks even when using shared snapshots.

## Required workflow shape

Function invocation: authenticate and authorize, resolve the deployment, atomically
admit the task and durable demand, wake execution planning, request container
placement when needed, assign eligible capacity, execute, commit the result and
usage, then publish observations. Capacity shortage leaves accepted work queued
according to its deadline and policy. Admission rate and execution rate differ.

Cron: identify a due occurrence, use the same admission rules, and atomically record
its outcome with schedule advancement and any accepted task/demand. Notification
failure after commit must not turn acceptance into apparent rejection.

HTTP endpoints: authorize and admit the request, claim a container with available
concurrency, forward the stream, and release/complete the claim. A cold endpoint
raises demand and waits within an explicit deadline. Retirement closes admission
and drains or applies the supported termination policy under the execution owner.
Do not decide idleness from a stale snapshot and stop unconditionally.

Container startup: execution creates durable intent, scheduling selects capacity,
the host receives a fenced assignment, prepares required bytes/mounts, starts the
container and reports readiness. Registration, image preparation, launch and user
readiness are separate stages. Cache bytes by verified identity and avoid repeated
preparation when the same ready artifact can be reused safely.

Host connection: compute validates identity and enrollment, establishes scoped
authority and worker eligibility, then accepts observations. The transport owns
connections and streaming. Host code must not import backend persistence or obtain
broad database/operator credentials.

Deployment change: control commits desired configuration and necessary durable
effects. Execution stops admission, drains/cancels supported work and retires
containers; compute releases unneeded capacity. Each owner performs its cleanup.
A coordinator must not know another owner's Redis key layout.

Image builds, checkpoints and disk operations retain their distinct artifact and
recovery guarantees while using existing execution, storage and compute owners.
They do not acquire private copies of these owners' decision logic.

## Contracts and Python integration

Define clear new resource APIs, error semantics, pagination, streaming, deadlines,
authorization and identifiers. Coordinate the Python SDK, CLI, runner and web with
the new definitions; their old models are references, not permanent constraints.

Use HTTP/JSON with OpenAPI for public administration and SDK operations, ordinary
HTTP/WebSocket forwarding for workload traffic, and a typed internal host protocol.
Each wire contract has one source. Generate bindings at actual language boundaries
rather than copying schemas manually. Do not expose internal Go types directly.

Domain types are not automatically HTTP DTOs or database rows. Share representations
where meanings match; introduce conversion only for a real boundary. Keep typed
contract examples that both Go and retained consumers can validate.

JSON and ordinary platform operations must be usable without Python. Python source,
handler references and cloudpickle payloads remain explicit Python capabilities.
Do not require future SDKs to deserialize Python objects, or silently weaken current
Python execution semantics. Python user code continues to run in Python.

User code reaches the platform through a local runner protocol, a language-neutral
contract between the host runtime and a per-language runner in the container. Go
implements the work shared by every language: input delivery, result and log
transfer, heartbeats, process slots, cancellation, draining and endpoint
forwarding. A runner loads user handlers, invokes them, serializes values in its
language and reports user failures. The Python runner is the first implementation.
Go and TypeScript SDKs can serve the same protocol with a small runner or from the
user's program without backend changes. The first execution slice decides whether
the shared work runs in the agent or in a Go supervisor inside the container.

## Development and acceptance

Follow AGENTS.md for Go package boundaries, error handling, goroutine lifetimes,
bounded work, tooling and test standards. Pin concrete library/toolchain versions with the
first implemented workflow; document changes to the choices below in its task.

Before implementing an assigned area, trace it in the reference through clients,
owners, stores, queues, hosts and providers. Include failure, retries and cleanup.
Name the final owner and list the decisions, duplicate state, conversions, reads,
RPCs, branches and abstractions that the replacement eliminates.

Implement a complete public outcome. Remove superseded machinery in the same task.
A renamed service, ported class hierarchy or extra coordination layer is not the
requested result. Do not carry an oversized framework into Go behind interfaces.

Reuse existing acceptance evidence where it tests public outcomes. Rewrite tests
that depend on old implementation shape. Test transaction/concurrency guarantees
against real PostgreSQL/Redis and host behavior against real containers. Cover
cancellation, stale ownership, duplicate delivery, partial failure and recovery.

Measure comparable completed capabilities:

- Admission and placement throughput, backlog age and fairness with sufficient
  capacity and with capacity deliberately unavailable.
- Capacity acquisition, agent startup, image preparation, container readiness and
  user execution separately.
- Warm endpoint latency, cold-start latency, streaming, concurrency and drain.
- Idle recurring queries/bytes, active batch round trips, memory/CPU and contention.
- Growing workloads, including 2,000 jobs and beyond the first saturation point.
  A batch size such as 100 is a tuning bound, not a scale limit or capacity promise.
- Multiple scheduler replicas and growing history, without multiplying redundant
  scans or confusing added workers with increased scheduling throughput.

Record baseline and replacement conditions, representative latency distributions,
throughput and failures. Reuse scoped snapshots and perform real batched operations.
Keep recurring work proportional to relevant live/due resources and measure global
fairness queries that still inspect large populations.

Compare handwritten production code and required operational machinery for the
same capability. Report generated code, tests and external dependencies separately.
Do not count unfinished functionality or moving code into dependencies as savings.
Reconsider net growth and explain indispensable new guarantees with evidence.

## Migration and delivery

Develop complete workflows in isolated local environments. The initial proof should
exercise Python deployment through admission, scheduling, host startup, execution,
result publication and cleanup. Expand capability by capability; this does not
require preserving the old implementation inside the new tree.

Use a new SQL migration chain for the fresh schema. Prefer explicit SQL and
transaction boundaries over porting the Python ORM/repository class hierarchy.
Do not add Alembic or Python backend packages to the new runtime. Old migration
history remains in the reference, without imposing its schema on the replacement.

Do not run old and new schedulers as simultaneous authorities over the same work.
Validate the new clients, feature coverage, rollout, rollback limits and cleanup
before cutover. Any data import needs a separate explicit design. Deployment automation is rebuilt only for implemented services and remains
isolated from production until authorized. CI must never imply missing backend
acceptance passed.

Completion requires user-visible parity with the reference, preserved guarantees,
simpler implementation, measured performance and removal of superseded paths. Record gaps and leave the
area unchecked until its acceptance is complete. All items below concern the new
implementation; previous Python rewrite checkmarks do not carry over.

## Agent prompt

Read this document and applicable AGENTS.md files. Investigate the assigned area
against the pinned reference and its current consumers. List the reference's
user-visible behavior for the area, since the rewrite matches it one to one.
Propose the simplest workflow consistent with these owners, identify what
disappears, and resolve material contract/architecture conflicts before
implementation.

When implementation is requested, complete the owner and necessary cross-owner
changes, coordinated client updates, real acceptance and before/after evidence. Keep
unrelated work separate. Do not add placeholders, alternate implementations or
future frameworks. Report the outcome, deletions, measurements and remaining gaps.
Update the assigned checklist item only when its completion requirements are met.

## Areas

- [ ] Architecture boundaries, process composition and configuration
- [ ] Public contracts and generated cross-language bindings
- [ ] Database access, fresh schema and durable recovery
- [ ] Authentication, authorization and scoped credentials
- [ ] Users, workspaces and invitations
- [ ] Apps, deployments, releases and desired configuration
- [ ] Task admission, lifecycle, retries and cancellation
- [ ] Function execution, local runner protocol and Python runner integration
- [ ] Container lifecycle, assignment and draining
- [ ] Workload autoscaling
- [ ] Scheduling, placement and fairness
- [ ] Compute capacity and fleet management
- [ ] Cron jobs and scheduled admission
- [ ] HTTP endpoints, WebSockets and streaming
- [ ] Pods and devboxes
- [ ] Shells and SSH
- [ ] Image builds and distribution
- [ ] Agent enrollment, updates and machine lifecycle
- [ ] Host runtime and local worker supervision
- [ ] Worker commands, reporting and credential scope
- [ ] Gateway, routing and tunnels
- [ ] Billing and payments
- [ ] Usage and metering
- [ ] Object storage and artifacts
- [ ] Volumes, disks and attachments
- [ ] Checkpoints, retention and cleanup
- [ ] Cache and source cache
- [ ] Maps and queues
- [ ] Secrets
- [ ] Custom domains
- [ ] Events, logs and metrics
- [ ] Notifications
- [ ] Python SDK and public CLI integration
- [ ] Web API integration and data flow
- [ ] Operations and administration
- [ ] Local development, CI and deployment
- [ ] Platform acceptance, scale evidence and cutover
