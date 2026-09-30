# Wave 2: designs after function execution

These designs build on tasks/function-execution.md. Each becomes its own task
file and packet once slice 1 is integrated. Reference findings come from the
traces of 9e259ce75.

## Container API

Containers hold no platform credential. In-container SDK calls (spawn,
queues, maps, artifacts, task context) go to a local HTTP endpoint served by
the supervisor on a Unix socket in /run/lazycloud. The supervisor forwards
each call to the agent over ContainerLink, and the agent sends it to the
server as a host call tagged with the container id and the current attempt.
The server authorizes it against the container's assignment and the
release's workspace. This replaces the 24-hour workspace token the reference
put in every container, and the per-task 250 ms monitor polling.

## Cron

`cron_schedules(workload_id, expr, next_fire_at, enabled)` belongs to control.
The scheduler claims due rows with `FOR UPDATE SKIP LOCKED` in bounded
batches. In one transaction it admits a task through execution admission
with a unique `(schedule_id, scheduled_for)` and advances `next_fire_at` from
`scheduled_for`. Missed occurrences collapse to one run, as in the
reference. Duplicate runs become impossible: the reference could run one
occurrence twice through Redis cadence overlap and a crash window.

## Secrets

Per-secret data keys are wrapped by a master key held outside PostgreSQL:
KMS in production and a local key file in development. Release specs name
secrets. When the server builds StartContainer it resolves exactly those
names and sends them as container environment over the host session. The
supervisor redacts them from output. Read-scope tokens cannot read values.
The reference derived keys from a plaintext column in the same database, and
it let workers request any secret name.

## Images

Global identity is the spec digest over the base image digest, steps and
context digest. Builds run as BuildKit job containers placed by scheduling,
with registry cache import and export, and publish OCI images by digest to
the registry. Hosts pull by digest through the Docker Engine using a
short-lived registry token sent over the session. This deletes clip/FUSE lazy
loading, the cache-server, .rclip archives, composite publishers and
skopeo. Build steps under gVisor need a spike; dedicated build hosts are the
fallback. Cold starts must be measured per phase with the existing startup
phases.

## Endpoints

A Go edge (in the server binary until scaling says otherwise) resolves host
names from an in-memory route table rebuilt on NOTIFY. It authenticates,
strips caller credentials and forwards each request as its own stream on a
data connection the agent opens to the edge, separate from the control
session. The agent forwards to the supervisor, which enforces slot
concurrency and returns an explicit busy result. In-flight counts are
in-memory per edge. Edges publish per-release in-flight leases with a TTL
for autoscaling and keep-warm, and execution alone decides scale-up. A cold
request records demand, waits for the container-ready NOTIFY and gives up
at its deadline. No task or dispatch row is written per request; usage is
aggregated. Custom domains keep Cloudflare for SaaS and route only verified
hostnames.

## Volumes, disks, artifacts, queues and maps

- Volumes: an S3 prefix per volume, mounted per workspace on the host by the
  agent with short-lived credentials from the session, and bind-mounted into
  containers. `volume_mounts` rows let deletion check live use
  transactionally.
- Disks: keep disk-engine, run by the agent. The lease row is tied to the
  container id and fenced by execution container state. The EBS cache is
  dropped for v1.
- Artifacts: an `artifacts` table, uploaded with presigned multipart through
  the container API, so the attempt is bound by authority rather than by a
  client-supplied task id.
- Queues and maps: PostgreSQL tables. Pop is `DELETE ... SKIP LOCKED`
  with a NOTIFY for blocking pops, and maps use revisions for compare-and-set
  plus a swept `expires_at`. Both are reached only through the container API
  or public API.

## Billing and usage

Usage comes from container lifetimes that execution already records with
server timestamps: `ready_at`, `stopped_at`, the resource shape, and the
last heartbeat after a host loss. An aggregator advances a durable
`billed_through` cursor per container in batches, writing contiguous
`usage_intervals` keyed by `(container, from)` and priced into immutable
`ledger_entries` unique by source. Balances are an idempotent rollup behind
a per-account advisory lock and are never computed inline on ingest.
Stripe is only the payment provider: customers are created lazily, and free
accounts have no subscription. Events are stored first and processed
asynchronously, and no Stripe I/O runs under a database lock. `Admit` is one
function inside the execution admission and planning transactions. The
reference billed from 5-second worker windows with an in-memory cursor
(double charges or lost tails), locked the account row per record, and
required a $0 Stripe subscription at sign-in.

## Compute fleet

`hosts` carries the provider, market, shape and a launch token equal to the
host id. One capacity controller, holding an advisory lock, computes the
shortfall from pending containers, runs first-fit-decreasing over the
cheapest offers and inserts `requested` hosts. A launcher calls
`RunInstances(ClientToken=host.id)` directly, with no ASGs. Insufficient
capacity writes a cooldown row. Preemptible demand tries Spot first. Each
market keeps a running headroom floor, and stopped or hibernated reserves
wait for data that justifies them. The agent polls IMDS and reports
interruptions over the session, and the server drains in one transaction.
Cloud hosts enroll with an STS presigned identity proof, and joined hosts
with a join token. Updates use systemd with A/B release directories. Only
the four US regions are used.

## Pods, devboxes, sandboxes, shells and SSH

Every kind is a container row with a kind, keep-warm, expiry, SSH and disk
settings. The supervisor is PID 1 everywhere and grows Exec (PTY), Signal,
Wait, Files and Snapshot on ContainerLink. Shells are Exec with a PTY; the
framed shell server and HMAC password are deleted. SSH keeps the
supervisor's certificate server, with the CA and host keys stored per
workspace so they rotate independently. Connections reach containers
through the agent's data connection. Idle detection uses heartbeated
`container_leases` rows instead of Redis counters that never expire.
