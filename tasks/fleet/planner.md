# Planner pass

## Scope

The fleet planning pass: read one snapshot, run `PlanFleet`, write intents,
container waits and the published plan, under the capacity lock. It replaces
`PlanCapacity` and the idle drain in `Retire`, and removes what they
superseded.

Parity sections (tasks/fleet/parity.md): Coordination; the inputs to Forecast
and timing (arrivals, scheduled demand, activation p95, reported memory);
the intent writes for Demand coverage, Reserve lifecycle and Retention,
consolidation and rightsizing.

Owns:

- `migrations/0005_fleet_markets.sql`: table `fleet_markets` (see plan.md);
  `hosts.light_since`. No seed rows: the pass upserts.
- `internal/compute/capacity_controller.go`: the pass. Rename it if the
  name no longer fits; delete `cheapestPerContainer`, `purchasePlan` and the
  first-fit purchase loop.
- `internal/compute/queries/planner.sql` (new): move `TryCapacityLock`,
  `InFlightHosts`, `PendingDemand`, `LiveCloudHostCounts`, `ActiveCooldowns`,
  `HostingConnections`, `InsertRequestedHost`, `SetCapacityWaits`,
  `MarkIdle`, `IdleHosts` and `DrainHosts` here from fleet.sql, then add the
  snapshot reads and intent writes.
- `internal/compute/retirement.go` after the provider packet moves
  reconcile out: `Retire` keeps connection drains and `ClaimTerminations`;
  the headroom and idle-timeout drain goes.
- `internal/compute/offers.go`: delete `catalog`, `spotDiscount`,
  `regionOrder` if unused, `regionPremium`, `offersFor`, `price` and the
  `HeadroomFloor` field once the policy packet's functions replace them.
- `internal/compute/fleet_config.go`: drop `LAZYCLOUD_FLEET_HEADROOM`.
- `cmd/scheduler/main.go`: the pass cadence. Minimal edits.
- `deploy/helm/lazycloud/environments/prod.yaml` and `deploy/README.md`:
  drop the headroom setting.
- Tests: `capacity_controller_test.go`, `retirement_test.go`,
  `capacity_bench_test.go` kept compiling and meaningful.

Migration: 0005. Protobuf and OpenAPI: none.

Depends on: the policy, provider and agent-resume packets merged into
fleet-capacity-plan.

## Plan

1. Snapshot queries, each a fixed statement independent of backlog size:
   - platform AWS hosts not deleted, with load from live containers, pinned
     container count (non-preemptible or machine-pinned), reserve columns,
     `light_since`, cost;
   - pending demand, up to 2,000 oldest, grouped in SQL by shape and
     placement with counts and container ids. Rewrite the existing
     `array(select jsonb_array_elements_text(...))` in `PendingDemand` into
     a Neki-safe form if the router rejects it;
   - platform container arrivals of the last 600 s, aggregated by market and
     shape, bounded by a uuidv7 id range on the primary key;
   - schedules due within the provision horizon joined to their release
     resources;
   - activation p95 by kind and hardware over the last day (an aggregate,
     not a window function over a join);
   - reported memory by nominal shape from ready hosts;
   - cooldowns with `refused_at`, fresh Spot prices, `fleet_markets` rows.
2. Call `Forecast`, `RankOffers` and `PlanFleet`; write the actions as the
   intent table in plan.md says, in the same transaction: insert requested
   hosts, set `resuming` with `resume_requested_at`, set `preparing` with
   `reserve_mode`, drain, set `terminating`, write container waits, update
   `light_since` only where it changes, upsert `fleet_markets` with the
   market plan and a 5-minute expiry. NOTIFY `lc_compute` once.
3. Consolidation: drain the chosen host through
   `Containers.DrainHostContainers`; record `consolidating_host` and its
   start; clear it when the host empties or after 3,600 s; set the 900 s
   cooldown.
4. Cadence: run on every `lc_compute` wake for demand; reserve growth at most
   every 20 s while `pressure_since` is 5 s old, else every 60 s. Log one line
   per market when its decision changes.
5. Remove the superseded code and settings; update the benchmark to the new
   pass.

## Progress

Branch `fleet-planner` from `fleet-capacity-plan` at
`7f33b8bc59af460edc8e5a2276c2588c8aa9d7ce`.

## Intentional differences

- One pass covers demand and reserves; the reference ran acquisition and
  reserve planning as separate services.
- The published plan is a PostgreSQL row per market, not a Redis key.

## Evidence

## Gaps and unverified boundaries

## Verification

- Owner tests with real PostgreSQL for each intent and for the rules that
  span a pass: resume before buy, borrowing withheld without a current plan,
  pending launches never justify retirement, idle host returned to the
  reserve when it is short, retirement waits for a plan, one consolidation
  per market, the 16-action cap.
- A cross-owner test inside compute with the AWS emulator and the hostsession
  test harness: a pending container resumes a stopped reserve, the actuator
  starts it, the host says Hello, scheduling places the container.
- Neki-safe SQL review of every new statement (rules in README.md).
- `EXPLAIN (ANALYZE, BUFFERS)` of each snapshot query at 2,000 and 10,000
  pending containers; buffers must stay flat beyond the batch.
- `go test -race ./internal/compute ./internal/scheduling ./cmd/scheduler`,
  gofmt, go vet, golangci-lint, `./check.sh`.
- Real EC2 in `AWS_PROFILE=default-test`, us-east-2, after `aws sts
  get-caller-identity --profile default-test` confirms the disposable
  account: run the pass and actuator against a scratch database with the
  default VPC as the fleet network and `MaxHosts` 2; insert a pending
  container; confirm the pass buys the planned offer with the planned
  options, then mark the market quiet and confirm it retires the host
  (terminate, Spot request cancelled). The host cannot enroll there; the
  full path runs on prod in the acceptance packet. Clean up every instance.

## Brief

```text
You own the planner packet of the fleet capacity work for LazyCloud (repo
github.com/AmbientWare/lazycloud). Work alone; do not start sub-agents.
Create branch `fleet-planner` from origin/fleet-capacity-plan after the
policy, provider and agent-resume packets merged; record the SHA in
tasks/fleet/planner.md.

Read first: AGENTS.md, tasks/fleet/README.md, tasks/fleet/plan.md,
tasks/fleet/parity.md, tasks/fleet/planner.md, and the policy, provider and
agent-resume packet files for what they delivered. The reference is commit
9e259ce75: read it with `git show 9e259ce75:<path>` for behavior only; never
read its env or credentials. Start with
packages/compute/src/compute/reserve_planning.py,
packages/database/src/database/repositories/fleet_demand.py and
packages/scheduler/src/scheduler/reserves.py.

Goal: deliver the parity lines marked "Packet: planner" one to one, wired
through the policy packet's pure functions and the provider packet's
actuator, implemented better: one pass, a fixed number of statements per
pass, intents in one transaction, Neki-safe SQL. Delete superseded paths in
the same change. Record differences in tasks/fleet/planner.md.

You own the files under "Owns" in tasks/fleet/planner.md and migration 0005.
Stay off the policy packet's fleet_*.go files (propose changes instead),
launcher.go, reconcile.go, the actuator, the host protocol files,
fleet_admin.*, internal/api and web/. A change elsewhere is a `Propose: ...`
commit, explained in your report.

Environment: go1.27.1 (export GOTOOLCHAIN=go1.27.1 if needed); `go tool
sqlc generate` after query edits. Test PostgreSQL: `docker compose -f
compose.test.yaml up -d --wait`; shared, never stop it.

Verify: the tests, plans and real EC2 run under Verification in
tasks/fleet/planner.md, in AWS_PROFILE=default-test only, after sts
get-caller-identity confirms the disposable account. Clean up every
instance and Spot request, and say so in your report.

Commit and push after each meaningful step, one-line subjects, no trailers.
Don't open PRs. Report in under 500 words: parity lines delivered with test
names, pass cost at each backlog size, proposed shared changes, gaps.
```
