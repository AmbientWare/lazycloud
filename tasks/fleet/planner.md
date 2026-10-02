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
`7f33b8bc59af460edc8e5a2276c2588c8aa9d7ce`. All five plan steps are done.

- `Compute.Plan` (internal/compute/planner.go, planner_snapshot.go,
  planner_markets.go, queries/planner.sql) replaces `PlanCapacity` and the
  idle drain in `Retire`. One transaction under the capacity lock: 12 reads,
  then PlanFleet for the platform and for each connected account, then one
  statement per intent kind, NOTIFY `lc_compute` once and `lc_host` for
  returned hosts.
- Deleted: `capacity_controller.go` (`purchasePlan`, `cheapestPerContainer`,
  the first-fit buy loop), the old `catalog()`, `InstanceType`, `Offer`,
  `Target`, `offersFor`, `price`, `spotDiscount`, `regionPremium`,
  `zoneIn`, `Fleet.HeadroomFloor`, `LAZYCLOUD_FLEET_HEADROOM` (config, prod
  values, deploy README), `MarkIdle`, `IdleHosts`, `InFlightHosts`,
  `LiveCloudHostCounts`, `hosts.idle_since`.
- The launcher's RAM lookup, the Spot price type list and the quota vCPU
  counts read `FleetCatalog` (one function body each in launcher.go,
  spot_prices.go and quotas.go).
- Migration 0005: `fleet_markets`, `hosts.light_since`, drop
  `hosts.idle_since`, index `containers_arrived (id) where state <>
  'pending'`.
- `Propose:` commits: `OfferInputs.OwnerPays` and `OfferInputs.PlainStop`
  in fleet_offers.go (with a test, and PlainStop in fleet_plan.go's
  hibernation shapes); the `HeadroomFloor` warm target in fleet_admin.go.

Parity lines marked "Packet: planner", with tests (internal/compute):

- Forecast inputs (arrivals, scheduled demand, activation p95, reported
  memory): `TestRecentArrivalsAndDueSchedulesRaiseTheTargets`,
  `TestPlannedCapacityUsesTheMemoryHostsOfTheTypeReported`,
  `TestAColdBootAfterAHibernationStopsTheTypePlainly`.
- Backlog: intentional, execution creates pending containers.
- Resume before buying, current release only, borrowing:
  `TestPendingWorkResumesAReadyReserveBeforeBuying`,
  `TestSpotWorkResumesAnOnDemandReserveOnlyAboveTheTargetThisPassComputes`,
  `TestAPendingContainerResumesAReserveThatJoinsAndTakesIt` (actuator
  start, Hello through `OpenSession`, placement).
- Covering, 16-action cap, pending launches, waits and the limit:
  `TestCapacityBuysTheCheapestOfferEachContainerAccepts`,
  `TestCapacityPacksDemandOntoOneHostAndCountsHostsInFlight`,
  `TestGrowthStopsAtSixteenActionsPerMarketAndPass`,
  `TestPendingLaunchesNeverJustifyRetiringServingHosts`,
  `TestFleetLimitAndPurchasesExplainPendingTasks`,
  `TestABoughtHostThatCannotTakeItsContainerCoolsItsOffer`,
  `TestQuotaRoomsAndSpotPricesSteerPurchases`.
- Reserve intents: buy for reserve, return to reserve (hibernating), drain
  when it cannot stop, surplus retirement, refresh, stuck preparing:
  `TestAReservePassBuysTheFloorsAsServingHostsAndHibernatingReserves`,
  `TestAnIdleHostReturnsToTheReserveWhileTheReserveIsShort`,
  `TestIdleHostsLeaveOnlyBeyondTheWarmTargetAndTerminate`,
  `TestSurplusReservesRetireAndAStaleOneRefreshes`,
  `TestAReserveWhoseAgentNeverAnswersFails`.
- Retention waits for a plan; consolidation one per market, cooldown,
  release once empty: `TestRetirementWaitsForTheReservePass`,
  `TestOneConsolidationPerMarketUntilItsHostEmpties`.
- Coordination: cadence, lease, published plan with expiry, one log line
  per changed decision: `TestAShortMarketBringsTheReservePassForward`,
  `TestConcurrentCapacityPassesBuyEachHostOnce`,
  `TestThePassPublishesEachMarketAndLogsOnlyChangedDecisions`.
- Connected accounts keep today's fleet: `TestAConnectedAccountsIdleHostLeavesAfterTheIdleTimeout`,
  `TestConnectionHostsLaunchTheBakedImageSharedWithTheirAccount`.
- Pass cost: `TestPlanningPassCostStaysFlatAsBacklogAndHistoryGrow`.

## Intentional differences

- One pass covers demand and reserves; the reference ran acquisition and
  reserve planning as separate services. Every pass (each `lc_compute` or
  `lc_execution` wake, and the 5 s fleet tick on the leader) acts on
  pending demand: resumes and purchases for pending containers, waits,
  light-use clocks, pressure and consolidation bookkeeping. Elective
  growth, retention, return to reserve, retirement, consolidation,
  rightsizing and refresh run, and the plan is published, only on a
  reserve pass: no plan yet, the newest `generated_at` 60 s old, or 20 s
  old while a market's `pressure_since` is 5 s old. The cadence lives in
  `fleet_markets`, so it survives a leader change.
- The published plan is a PostgreSQL row per market, not a Redis key.
  "Discretionary retirement waits for a current plan" holds because only
  the pass that publishes the plan retires; the targets it uses are the
  ones it publishes, so there is no missing or expired plan to read.
- Connected accounts plan through PlanFleet with no headroom or reserves,
  no consolidation (light use at 0% is idle) and the idle timeout as the
  light-use wait, with `OwnerPays` (no platform margin, as
  pool_provider.py:122-123). Their Spot offers use the platform's Spot
  quotes, so a connection zone the platform does not price buys on-demand;
  the rewrite estimated Spot at 40% of on-demand there.
- A consolidation cordons the host (`capacity_state` draining, reason
  `consolidating`, phase still ready) and drains its containers through
  `DrainHostContainers`; once it empties it is uncordoned and retention
  drains it or returns it to the reserve, as the reference's idle drain
  did. Its `light_since` is kept while it drains.
- A reserve retired while still preparing drains (preparing -> ready ->
  draining in one guarded statement, both transitions checked); one not
  launched yet is removed; one launching is decided again next pass.
- A host whose agent refused to prove a stop serves with its reserve mode
  set (reserve_session.go). The pass treats it as unable to stop, so
  retention drains it rather than asking again, and, while it is idle in
  the cooldown window after the refusal, cools its offer without a
  `refused_at`. A failed stuck reserve cools its offer the same way.
- A reserve being prepared or stopping holds host room as well as reserve
  room, and a reserve purchase needs host room (`Propose:` commit in
  fleet_plan.go), so running hosts stay within `MaxHosts`.
- Quotas are read raw on the pass's transaction; PlanFleet subtracts the
  snapshot's own hosts. `QuotaUsage` and the exported `QuotaRooms` are
  gone; Spot prices are read on the same transaction.
- A host stuck in `preparing` for 15 minutes outside an agent update fails
  as `service_lost`, and reconcile terminates its instance (plan.md's
  `any -> failed` for lost service). The pass owns it because no other
  loop watches that phase.
- A bought host that joined and still cannot take its container cools its
  offer without a `refused_at`, so it does not count toward region
  cooling.
- P1's unreliable mark: a `fleet_activations` resume with outcome
  `cold_boot` in the last day makes reserves of that type and region stop
  plainly (`PlainStop`), so a market keeping a hibernation target drains
  such a host rather than returning it.
- Scheduled demand counts each schedule's next fire only: compute cannot
  import the cron parser (schedules -> execution -> compute), and a cron
  firing more than once within the 6-minute horizon has the arrival
  history the occupancy forecast already counts.
- GPU arrivals are forecast in the market of the card placement gave them;
  pending GPU work in its first reserved card, a stocked one first for
  "any".
- A market new since the last reserve pass is published on the next one.

## Evidence

- `go test -race ./internal/compute ./internal/scheduling ./cmd/scheduler
  ./internal/hostsession ./internal/api ./internal/execution` pass;
  `./check.sh` passes; golangci-lint 0 issues.
- `acceptance/neki/check.sh`: 682 checked, 0 router failures. The planner's
  ordinary errors are the jsonb_to_recordset statements given a non-array
  dummy and `NotifyHosts` given an empty channel. The first run failed
  `ReturnToReserve` ("subqueries in UPDATE FROM WHERE"), since rewritten.
- `/tmp/nekicompat/check-neki.sh .`: rejections are only the columns and
  tables 0003-0005 add that prod lacks.
- Pass cost (`TestPlanningPassCostStaysFlatAsBacklogAndHistoryGrow`, a
  reserve pass over 100 serving hosts with 4 live containers each, 10 due
  schedules; max shared buffers of any node per snapshot read):

  | Pending, finished | Statements | Wall | hosts | pending | arrivals | scheduled |
  | --- | --- | --- | --- | --- | --- | --- |
  | 0, 1,000 | 17 | 51 ms | 522 | 2 | 164 | 105 |
  | 500, 1,000 | 17 | 41 ms | 446 | 19 | 67 | 93 |
  | 2,000, 1,000 | 17 | 58 ms | 349 | 82 | 18 | 93 |
  | 10,000 plus 10,000 on a warm cron function, 20,000 | 17 | 62 ms | 349 | 123 | 18 | 93 |

  Activation stats, cooldowns and markets stay at 0-5 buffers. No node
  reads more than the 2,000-container batch. The guard first caught the
  scheduled read counting a release's pending containers (1,294 -> 3,376
  buffers) and the arrivals read walking the pending backlog; both now read
  only placed containers.
- `BenchmarkPlan` (2,000 pending, Spot quotes): 43 ms p50, 67 ms p95, 18
  hosts. Before, `BenchmarkPlanCapacity` on 7f33b8bc: 144 ms p50, 191 ms
  p95, 288 one-container hosts in one pass and about 300 statements (one
  insert per host). The new pass packs onto larger hosts and stops at 16
  growth actions per market, so the host counts are not the same capability.

## Gaps and unverified boundaries

- Real EC2 did not run: the `default-test` role lacks the EC2 describe,
  SSM and S3 permissions. `TestRealEC2PlansPurchasesAndRetiresTheSurplus`
  is gated on `LAZYCLOUD_EC2_ACCEPTANCE_PROFILE`: it buys two Spot hosts
  from live Spot prices, checks the launched type, zone and market, drains
  the surplus once quiet and checks it terminates with its one-time
  request closed. Nothing was created.
- The full path (agent prepares, actuator stops, resume joins) is proven
  only against the AWS emulator and `OpenSession`; the gRPC session's half
  is agent-resume's tests. It runs on prod in the acceptance packet.
- acceptance/neki gives jsonb parameters a non-array dummy, so the
  jsonb_to_recordset writes are routed but not executed with rows there;
  the owner tests execute them.
- A cron firing more than once within the horizon counts once (above).

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
