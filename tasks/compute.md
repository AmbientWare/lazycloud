# Compute packet

Parity section "Compute (managed, AWS connect, joined machines)" in
tasks/parity.md, plus agent install, enrollment, updates and interruptions.
Migration `migrations/0008_compute.sql`, protobuf fields 70-79.

Outcome: `lazycloud machine join --name gpu-1 --workspaces dev` on a Linux
host runs the agent, the machine shows `ready` in `machine list`, and
`@app.function(machine="gpu-1")` runs there and nowhere else. A workspace on
platform compute gets EC2 capacity launched for pending containers that fit
no host, and idle instances drain and terminate. `lazycloud cloud connect
aws` and `cloud authorize` put the same fleet in a customer account.

## Reference findings (9e259ce75)

- Placement is one of `platform`, `connection:<id>` or `machine:<id>`,
  resolved from the workspace's location and `machine=`. Unpinned workloads
  never run on joined machines; a joined machine runs only workloads pinned
  to it from the workspaces it serves.
- The managed fleet used launch templates, Auto Scaling groups, retained
  pools, stopped/hibernated reserves and a reserve planner with forecasts,
  margins and consolidation, spread over a few thousand lines. Instance
  identity was an STS presigned GetCallerIdentity URL verified server-side.
- AWS connections were a 2,300-line provider module plus a 1,500-line
  lifecycle with tombstones, leases and generations. The platform created the
  node role through the connection role after validation.
- Agent self-update was driven by a release file on the gateway and a shell
  supervisor that rolled back after 60 polls without a commit.
- Spot interruption: the agent polled IMDS and posted to the gateway; the
  scheduler drained before the deadline and preempted after it.

## Design

### Ownership and state

- Compute owns hosts, connections, authorizations, offers, provisioning,
  retirement, interruptions and agent releases. Scheduling owns which host a
  pending container goes to; execution owns container and attempt state.
- `hosts.state` stays the session authority (online, offline, lost,
  retired) that liveness and assignment fence on. `hosts.phase` is the
  lifecycle users see. Placement takes hosts that are `online`, `ready` and
  `available`.
- `hosts.kind` is `platform`, `connection` or `machine`. A container's
  target is derived at placement time: `machine` from its release's
  `placement.machine`, else its workspace's `connection_id`, else platform.
  Region, zone, market and GPU come from the release spec, so containers
  carry no copy of them.

### Joined machines

- `POST /v1/machines/join-command` needs an account credential, and every
  listed workspace must be owned by the caller. It creates the machine row
  (`requested`) or reuses one that never joined, replaces its workspaces,
  and mints a join token bound to it (30 minutes by default). A newer token
  replaces unused older ones.
- `Enroll` with a machine token sets the host token, capacity, GPUs and
  preflight, and moves to `joining`; a failed error-severity check moves to
  `failed/host_preflight_failed`. The first session moves to `ready`.
- Remove retires the host (`state = retired`, `phase = deleted`, token
  cleared) and stops its containers through execution in the same
  transaction, so running attempts retry. The agent's next call is refused
  and it exits with status 78, which its service does not restart.

### Managed and connected fleet

- One capacity controller (`pg_try_advisory_xact_lock('capacity')`) in
  cmd/scheduler simulates pending containers on ready and in-flight hosts,
  then packs the rest first-fit-decreasing onto the cheapest offers that
  satisfy each container (target, region, zone, market, GPU). It inserts
  `requested` hosts and marks containers `capacity_wait = provisioning`, or
  `limit` when the fleet limit holds them back.
- A launcher claims requested hosts with a lease and calls
  `RunInstances(ClientToken = host id)` outside any transaction. Spot is
  tried first for preemptible demand. Insufficient capacity or quota writes a
  cooldown row for the offer and fails the host, so the next pass picks
  another offer.
- Retirement: a ready cloud host idle past its idle window drains when its
  market keeps more idle hosts than its headroom floor, then terminates once
  empty. Reconciliation runs DescribeInstances by fleet tag: instances gone
  or stopped fail their hosts (containers released through execution),
  orphans are terminated, and hosts that never enrolled time out.
- Cloud hosts enroll with an STS presigned GetCallerIdentity request signed
  by the instance profile. The signature covers `LazyCloud-Host-Id`; the
  server checks the account, the node role, the session name equal to the
  host's instance id, and that the host is still provisioning or booting.
- Customer accounts: the connection role is assumed with the external ID and
  the same launcher runs with those credentials. The CloudFormation stack
  creates the connection role, the node role and instance profile, a VPC and
  public subnets.

### Interruptions and updates

- The agent polls IMDSv2 `spot/instance-action` and reports `Interruption`
  over the session. One server transaction marks the host draining with
  `preempting` capacity and drains its containers through execution: ready
  containers stop claiming, starting ones stop. The scheduler preempts what
  is still live 20 s before the reclaim time, so running attempts retry.
- `agent_releases` holds versions and per-architecture digests; one row is
  the target. A session whose agent is updatable and on another version gets
  `UpdateAgent`. The agent installs the release beside the current one,
  switches the `current` link, writes a trial marker and exits; its systemd
  wrapper rolls back to `previous` if the trial is not committed by a
  session within its start limit. Containers keep running across agent
  restarts, so updates need no drain.

## Plan

1. Contracts: migration, proto, OpenAPI, generated bindings. Done.
2. Compute owner: machines, join and enrollment, presence and phases,
   connections and authorizations, offers, capacity controller, launcher,
   retirement, reconciliation, interruptions, releases. AWS provider with
   aws-sdk-go-v2 behind consumer interfaces, tested against recorded
   responses at the provider boundary.
3. Scheduling: placement targets and constraints; pending reasons for
   `capacity_limit` and `provisioning_compute`.
4. Host session, API handlers, `/install/agent` endpoints, scheduler and
   server wiring.
5. Host runtime (parallel): agent subcommands and flags, GPUs, preflight,
   self-update, IMDS, cloud identity, install script, bundle.
6. Python (parallel): placement options in the SDK, compute, cloud and
   machine CLI on the new API, `workspace create --cloud aws`.
7. Integrated run: `machine join` on this host against a private stack,
   a pinned function, measurements; one cleaned-up EC2 check with
   `default-test` if it allows launches.

## Progress

- [x] Contracts
- [ ] Compute owner and AWS provider
- [ ] Scheduling constraints and pending reasons
- [ ] Host session, API, install endpoints, wiring
- [ ] Host runtime
- [ ] Python SDK and CLI
- [ ] Integrated run and measurements
