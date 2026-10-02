# Lifecycle and provider

## Scope

Everything compute does on EC2 for reserves, and the host phase table: the
reserve schema, launch options, start, stop, hibernate, forced stop, Spot
request cleanup, console evidence, reconcile of the new phases, Spot price
refresh, IAM and the node image.

Parity sections (tasks/fleet/parity.md): Provider (EC2); Node image; the
phase table in Reserve lifecycle; the Spot fetch, refusal cooldown and
`refused_at` lines in Offers, catalog and prices.

Owns:

- `migrations/0003_fleet_provider.sql`: `hosts.phase` check gains
  `preparing`; `hosts` columns `reserve_mode`, `hibernation_configured`,
  `spot_request_id`, `node_image`, `stop_requested_at`, `force_stop_at`,
  `stopped_at`, `resume_requested_at`, `image_evidence`, `evidence_checks`,
  `evidence_next_at`; table `spot_prices`; `capacity_cooldowns.refused_at`.
  Partial indexes for the actuator's claims (hosts in `stopping`,
  `resuming`, `terminating`). The migration reads and writes no table it
  creates.
- `internal/compute/types.go` (phase constants) and a new
  `internal/compute/host_phases.go` (the transition table from plan.md and
  the one checked phase write).
- `internal/compute/launcher.go`: hibernation options, root plus swap size,
  persistent Spot with stop or hibernate interruption and a tagged request
  when `reserve_mode` is set; record `spot_request_id`, `node_image`,
  `hibernation_configured`.
- `internal/compute/reserve_actuator.go` (new) and `queries/reserves.sql`
  (new): claim and perform StartInstances, StopInstances with or without
  `Hibernate`, forced stop, cancel-then-terminate for reserves, console
  evidence reads.
- `internal/compute/reconcile.go`: your first commit moves `Reconcile`,
  `reconcileRegions`, `reconcileRegion`, `hostGone` and `observedInstance`
  out of retirement.go unchanged; later commits teach it the reserve phases
  and orphan Spot requests. `Retire` and the rest of retirement.go belong to
  the planner packet.
- `internal/compute/spot_prices.go` (new) and `queries/prices.sql` (new):
  refresh and read.
- `internal/compute/aws.go`: error codes.
- `queries/fleet.sql`: only the launch, failure, cooldown, termination and
  reconcile queries (`ClaimLaunches`, `RecordLaunch`, `FailHost`,
  `InsertCooldown`, `ClaimTerminations`, `FleetHostsInRegion`,
  `FleetRegions`, `KnownHostIDs`, `SetHostPhase`, `MarkHostDeleted`). The
  planner packet moves the planning queries out of this file.
- `deploy/terraform/platform-deployment/iam.tf`: tag-scoped
  `ec2:StartInstances`, `ec2:StopInstances`, `ec2:GetConsoleOutput`,
  `ec2:CancelSpotInstanceRequests`; `ec2:DescribeSpotPriceHistory` and
  `ec2:DescribeSpotInstanceRequests`; `ec2:CreateTags` on
  `spot-instances-request` at `RunInstances`.
- `deploy/ami/node-setup.sh`: ec2-hibinit-agent and acpid, a check that the
  resume entry uses PARTUUID, `/sys/power/image_size` 0
  (bake.py:886-897).
- `cmd/scheduler/main.go`: one leader loop for the Spot price refresh and the
  actuator call inside the existing fleet loop. Minimal edits.

Migration: 0003. Protobuf and OpenAPI: none.

Depends on: nothing. Push the migration, phase constants and transition
table as your first two commits; the agent-resume packet branches from them.

## Plan

1. Move the reconcile code to reconcile.go unchanged. Add migration 0003,
   phase constants and `host_phases.go`; route existing phase writes through
   it.
2. Launch options. Hibernation only for catalog types that hibernate, RAM
   under 150 GiB, root volume 100 GiB plus RAM GiB. Spot reserves on
   persistent requests; serving Spot hosts stay one-time with terminate.
   Confirm on real EC2 that `InstanceInitiatedShutdownBehavior=terminate`
   is accepted with hibernation and with persistent Spot; drop it for
   reserves if EC2 refuses the combination.
3. Actuator. Claim hosts by phase with a lease (`FOR UPDATE SKIP LOCKED`),
   call EC2 outside the transaction, then record the outcome with a
   `from_phase` guard. Rules: no hibernate within 2 minutes of start;
   retry hibernation refusals for 10 minutes then stop plainly;
   `UnsupportedHibernationConfiguration` stops plainly at once; force a stop
   still pending after 10 minutes; StartInstances refused for capacity sends
   the host to `terminating` and writes a cooldown with `refused_at`; cancel
   a persistent request and confirm it launched this instance before
   terminating. Caps: 10 start, stop or terminate calls and 2 console reads
   per pass.
4. Reconcile. `stopping` and `stopped` instances of hosts in reserve phases
   are expected; `stopped` with `stop_requested_at` moves the host to
   `stopped` and sets `stopped_at`; a host EC2 stopped unasked still fails as
   `provider_stopped`. An orphan Spot request tagged for the fleet with no
   wanted host is cancelled.
5. Console evidence (default; P1 removes it if approved). After a host
   stopped by hibernation, read console output every 30 s, at most 12 times,
   looking for the agent's marker `lazycloud-sleep attempt=<id> boot=<boot>`
   followed by the kernel's hibernation entry and image-saved lines
   (retained_pool.py:233-256). Write `image_evidence`.
6. Spot prices. Every 5 minutes on the leader: per region, the catalog types
   it sells, `DescribeSpotPriceHistory` with `StartTime = now` per zone,
   upsert in one statement from arrays. Failures keep the last prices.
7. IAM and node image changes; bake a test image in `default-test` only if
   the user approves the cost, otherwise verify the recipe on a launched
   instance.

## Progress

## Intentional differences

- One actuator per host phase replaces pools, slots and their JSON state
  (retained_pool.py:91-135).
- Reserves exist only for platform hosts; connection hosts keep one-time
  launches and terminate on stop.

## Evidence

## Gaps and unverified boundaries

## Verification

- Owner tests with real PostgreSQL (`docker compose -f compose.test.yaml up
  -d --wait`; shared, never stop it) and the AWS emulator the package already
  uses (`aws_emulator_test.go`) for every actuator branch: hibernate accepted,
  refused then retried, refused past the deadline, unsupported, stuck
  stopping, Spot start refused, cancel before terminate, lost answers.
- Neki-safe SQL: no ARRAY(subquery), writable CTEs, subqueries in UPDATE SET
  or RETURNING, correlated LIMIT, subqueries in ORDER BY, window functions
  over joins, or DELETE ... USING.
- `go test -race ./internal/compute`, gofmt, go vet, golangci-lint,
  `./check.sh`, `terraform validate` in the deployment module.
- Real EC2 in `AWS_PROFILE=default-test`, us-east-2, after
  `aws sts get-caller-identity --profile default-test` confirms the
  disposable account. Through the launcher and actuator: launch an on-demand
  m7i.large able to hibernate, wait out the 2 minutes, hibernate, observe
  `stopped` and the evidence (user data writes the marker to /dev/kmsg, since
  the agent cannot reach a server there), start it, and record each
  duration. Then a persistent Spot m7i.large: stop, start, terminate, and
  confirm no Spot request remains. Tag everything `lazycloud:fleet =
  acceptance`; a cleanup sweeps instances and Spot requests with that tag
  even when the test fails.

## Brief

```text
You own the provider packet of the fleet capacity work for LazyCloud (repo
github.com/AmbientWare/lazycloud). Work alone; do not start sub-agents.
Create branch `fleet-provider` from origin/fleet-capacity-plan and record its
head SHA in tasks/fleet/provider.md.

Read first: AGENTS.md, tasks/fleet/README.md, tasks/fleet/plan.md (the
intent table and the state machine are your contract), tasks/fleet/parity.md,
tasks/fleet/provider.md. The reference is commit 9e259ce75: read it with
`git show 9e259ce75:<path>` for behavior only; never read its env or
credentials. Start with
packages/providers/aws/src/provider_aws/retained_pool.py, managed_pool.py
(launch template data), platform_pool.py, spot_prices.py,
packages/compute/src/compute/machine_lifecycle.py and deploy/ami/bake.py.

Goal: deliver the parity lines marked "Packet: provider" one to one,
implemented better: one actuator per host phase, idempotent EC2 calls
outside transactions, guarded phase writes. Record differences in
tasks/fleet/provider.md.

You own the files under "Owns" in tasks/fleet/provider.md. Your first two
commits are the reconcile move and the schema with phase constants; push
them at once, because the agent-resume packet branches from them. Stay off
capacity_controller.go, Retire in retirement.go, offers.go, the planning
queries, fleet_admin.*, internal/agent, internal/hostsession, internal/api,
contracts/ and web/. A change elsewhere is a `Propose: ...` commit,
explained in your report. Migration number 0003 only.

Environment: go1.27.1 (export GOTOOLCHAIN=go1.27.1 if needed); `go tool
sqlc generate` after query edits. Test PostgreSQL: `docker compose -f
compose.test.yaml up -d --wait`; it is shared, never stop it.

Verify: the owner tests and real EC2 run under Verification in
tasks/fleet/provider.md. Use AWS_PROFILE=default-test only, after sts
get-caller-identity confirms the disposable account; never the default
profile. Clean up every instance and Spot request you create, and list them
as gone in your report.

Commit and push after each meaningful step, one-line subjects, no trailers.
Don't open PRs. Report in under 500 words: parity lines delivered with test
names, measured durations, proposed shared changes, gaps.
```
