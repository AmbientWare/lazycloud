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
- A deploy or working-tree release pinned to a machine that does not serve
  the workspace is refused. A run (a working-tree release) pinned to a
  machine that is missing or not ready fails at admission and never falls
  back; a deployed call is admitted and waits for its machine
  (`TestRunsPinnedToAnUnavailableMachineFailAtOnce`).
- Remove retires the host (`state = retired`, `phase = deleted`, token
  cleared) and stops its containers through execution in the same
  transaction, so running attempts retry. The agent's next call is refused
  and it exits with status 78, which its service does not restart.

### Managed and connected fleet

- One capacity controller (`pg_try_advisory_xact_lock('capacity')`) in
  cmd/scheduler simulates pending containers on ready and in-flight hosts,
  then packs the rest first-fit-decreasing onto new hosts, each the offer
  with the lowest price per container among the cheapest that satisfy the
  container (target, region, zone, market, GPU). It inserts
  `requested` hosts and marks containers `capacity_wait = provisioning`, or
  `limit` when the fleet limit holds them back.
- A launcher claims requested hosts with a lease and calls
  `RunInstances(ClientToken = host id)` outside any transaction. Spot is
  tried first for preemptible demand. Insufficient capacity or quota writes a
  cooldown row for the offer and fails the host, so the next pass picks
  another offer.
- Retirement. A ready cloud host idle past its idle window drains when its
  market keeps more idle hosts than its headroom floor, then terminates once
  empty. Reconciliation runs DescribeInstances by fleet tag. Instances gone
  or stopped fail their hosts and execution releases their containers;
  reconciliation terminates orphans and times out hosts that never enrolled.
- Cloud hosts enroll with an STS presigned GetCallerIdentity request signed
  by the instance profile. The signature covers `LazyCloud-Host-Id`; the
  server checks the account, the node role, the session name equal to the
  host's instance id, and that the host is still provisioning or booting.
- Customer accounts. The launcher assumes the connection role with its
  external ID and runs with those credentials. The CloudFormation stack
  creates the connection role, the node role and instance profile, a VPC and
  public subnets.

### Host connection transport

- Agents dial the server with TLS and verify its certificate against the
  system roots, plus a PEM bundle from `--server-ca`. `--server-plaintext`
  is accepted only for a loopback server; any other address is refused with
  `ErrPlaintextRemote`, so host tokens and workload data never cross a
  network in the clear. Every agent connection to the server goes through
  one dial function, which the endpoints data connection should reuse.
- The server terminates TLS itself with `-grpc-tls-cert` and
  `-grpc-tls-key`, or serves plaintext behind an ingress that terminates
  TLS. Join commands and EC2 user data add `--server-plaintext` only when
  the server has no certificate and its agent address is loopback.
  `TestAgentDialsTheServerWithVerifiedTLS` runs against a real TLS listener.

### Interruptions and updates

- The agent polls IMDSv2 `spot/instance-action` and reports `Interruption`
  over the session. One server transaction marks the host draining with
  `preempting` capacity and drains its starting and ready containers
  through execution, so they claim nothing more. The scheduler preempts what
  is still live 20 s before the reclaim time, so running attempts retry.
- `agent_releases` holds versions and per-architecture digests; one row is
  the target. A session whose agent is updatable and on another version gets
  `UpdateAgent`. The agent installs the release beside the current one,
  switches the `current` link, writes a trial marker and exits; its systemd
  wrapper rolls back to `previous` if the trial is not committed by a
  session within its start limit. Containers keep running across agent
  restarts, so updates need no drain.

## Progress

- [x] Contracts: migration 0008, host fields 70-79, compute API, admin fleet API
- [x] Compute owner and AWS provider (aws-sdk-go-v2 against AWS endpoints)
- [x] Scheduling constraints and pending reasons
- [x] Host session, API, install endpoints, server and scheduler wiring
- [x] Host runtime: agent subcommands, GPUs, preflight, self-update, IMDS,
      cloud identity, install script, bundle
- [x] Python SDK and CLI
- [x] Integrated run on a private stack
- [ ] Real EC2 launch check, blocked by the `default-test` role's permissions

## Delivered

| Parity item | Evidence |
| --- | --- |
| `compute status`, `compute instances`, `compute workloads` | `test_public_cli_compute.py`; private stack run (below) |
| `cloud connect aws`, `authorize`, `validate`, `status --watch`, `reconnect`, `cancel-reconnect`, `retry`, `disconnect` | `test_public_cli_cloud_connection.py`; compute connection tests; private stack: connect returned the stack, validate recorded `assume_role_denied` from real STS, disconnect removed the unfinished setup; `aws cloudformation validate-template` accepts the template |
| `machine join` (all flags, foreground and `--background` systemd service) | `test_public_cli_compute.py`, `TestInstallScriptInstallsAReleaseAndRunsTheAgent`, `TestInstallScriptRefusesBadInput`, `TestServiceWrapperRollsBackAReleaseThatNeverConnects`, `TestServiceWrapperKeepsAReleaseThatCommits`; foreground join on this host |
| `machine list`, `machine update NAME --workspaces`, `machine remove ID` | CLI tests; private stack: update refused to drop a workspace whose deployment pins the machine; remove stopped the running task's container, retried the attempt and the agent exited 78 |
| Machine phases and offline badge (`connected`) | `hosts.phase`; owner tests; `machine list --json` |
| Placement `machine=`, `region=`, `availability_zone=`, `preemptible=` | `test_deploy_maps_gpu_and_placement_options`, `test_invalid_placement_options_fail_where_declared`; placement and capacity tests |
| GPU types and preference lists, plain A100 rejected | SDK tests; `TestAgentGivesContainersFreeGPUs`; a `gpu="any"` function pinned to the joined RTX 3090 saw `/dev/nvidia0` |
| `/install/agent`, `/install/agent/{os}/{arch}`, `/install/agent/{version}/{os}/{arch}` | API install tests; the join above downloaded the archive through them |
| Agent identity proof for cloud hosts | `TestCloudIdentityIsAPresignedCallerIdentityRequest`; `TestRealSTSVerifiesASignedHostHeader` against real STS with `default-test`: STS answered the proof and refused it re-addressed to another host |
| Spot interruptions, drain then preempt | `TestCloudHostEnrollsAndReportsSpotInterruptions`; interruption owner tests |
| Agent self-update with A/B releases and rollback | `TestAgentUpdatesItselfAndCommitsTheRelease`, `TestInstallReleaseRefusesBadReleases`, wrapper tests; a `systemd-run --user` unit rolled back a crashing release |
| Dashboard APIs: connected clouds, self-hosted machines, join dialog, admin fleet | Operations listed below |

## Integrated run

Private stack (compose project `lazycloud-compute`, ports 56xxx, state in
/tmp/lc-compute), server and scheduler from this branch, agent from a
release archive published with `server admin publish-agent-release`, on a
24-CPU host with an RTX 3090.

| Scenario | Result |
| --- | --- |
| `lazycloud machine join --name m2 --workspaces dev --foreground` to `ready` | 1.73 s, 80 MB archive over loopback |
| `@app.function(machine="m1")`, cold `.remote()` with the image cached | 1.57 s |
| Same, warm | 12-23 ms |
| Unpinned function in the same workspace | stays queued as `capacity_unavailable` because no platform host exists |
| `machine remove` with a running task | container stopped, attempt retried, agent exited 78 within 0.3 s |
| Capacity pass, 2,000 pending containers in 20 workspaces, two regions | 288 hosts requested; p50 120 ms, p95 130 ms (`BenchmarkPlanCapacity`) |

## API for the web packet

- Settings → Compute: `GET /v1/aws-connection`, `POST /v1/aws-connection`,
  `DELETE /v1/aws-connection`, `POST /v1/aws-connection/validate`,
  `POST|DELETE /v1/aws-connection/reconnect`, `POST /v1/aws-connection/retry`,
  `GET /v1/compute/instances`, `GET /v1/workspaces/{workspace}/compute`.
- Self-hosted machines and the join dialog: `GET /v1/machines` (account),
  `POST /v1/machines/join-command`, `PATCH /v1/machines/{machine}`,
  `DELETE /v1/machines/{machine}`. Live status comes from polling
  `GET /v1/machines`: `lifecycle`, `lifecycle_message`, `connected`,
  `preflight_checks` and `remediation`.
- Admin → Fleet: `GET /v1/fleet`, `GET /v1/fleet/nodes`.
- The "Upgrade to Business" gate reads billing's entitlements.

## Intentional differences

- Paths follow the new API (`/v1/aws-connection`, `/v1/machines`,
  `/v1/workspaces/{workspace}/compute`); collections page with `cursor` and
  `next_cursor`. `compute instances --json` and `compute workloads --json`
  print `{"instances": [...]}` and `{"workloads": [...]}`.
- Only the account that joined a machine may update or remove it; the
  reference let any member of its workspace remove it.
- A name that is already joined must be removed with `lazycloud machine
  remove` before it joins again; there is no `lazycloud-agent leave`.
- The install command names the gRPC address (`--server`) beside the
  download origin (`--gateway`). The script does not install Docker; it
  requires a working `docker info`. EC2 user data installs Docker first.
- The fleet has no Auto Scaling groups, launch templates or stopped and
  hibernated reserves. Instances launch with `RunInstances(ClientToken =
  host id)`; a new host is the offer with the lowest price per container
  among the twelve cheapest that take it. The admin fleet page reports zero
  reserve capacity and the idle headroom as the warm target.
- Agent updates follow the `agent_releases` target row and need no drain:
  containers keep running while the agent restarts.
- The connection stack also creates the node role and instance profile, and
  lives in us-east-2. An existing-role connection must provide an instance
  profile named `lazycloud-node`.
- Region names outside us-east and us-west are accepted and never get
  capacity, per product policy (only the four US regions are real).
- `machine join --gpu` seeds the machine's GPU model until the agent
  reports what it detects; the detected model wins.

## Gaps

- Real EC2 launch and termination are unverified. The `default-test` role
  lacks `ssm:GetParameters`, `ec2:DescribeImages` and `ec2:DescribeSubnets`,
  so no AMI or subnet could be resolved; RunInstances, TerminateInstances
  and DescribeInstances are covered only against recorded responses. Cloud
  enrollment and Spot notices on real EC2 are unverified for the same reason.
- `--background` installs as root under system systemd did not run here (no
  sudo); the unit and rollback wrapper ran under `systemd --user`.
- Plan gates (Business for connected clouds, Team for region pinning) belong
  to billing.
- Image builds for workspaces in a connected account run on platform hosts,
  and their volumes' bucket placement belongs to storage.
- Dashboard pages come with the web packet.

## Try it

1. `deploy/local/run.sh start` builds this tree's agent into an archive
   and publishes it with `server admin publish-agent-release`, the release
   joined machines install and update to. Elsewhere, run
   `deploy/agent/build-bundle.sh <dist> <version>`, serve `<dist>` with
   `LAZYCLOUD_AGENT_DIST_DIR` and publish the same way; the release pipeline
   (operations packet) calls these two steps.
2. `lazycloud machine join --name m1 --workspaces dev` on the host.
3. Deploy `@app.function(machine="m1")` and call it.
4. For EC2 capacity, set `LAZYCLOUD_FLEET_NETWORKS`, the node role and
   instance profile and AWS credentials on the scheduler and server.
