# Policy and offers

## Scope

Pure planning and ranking for the platform fleet: the policy, the catalog and
prices, offer ranking, covering search, forecast, activation estimates and
`PlanFleet`. No database, no AWS calls in production code, no wiring.

Parity sections (tasks/fleet/parity.md): Markets and targets; Forecast and
timing; Demand coverage and purchase; Offers, catalog and prices (all but
the Spot fetch and refusal mapping); the decisions in Reserve lifecycle and
Retention, consolidation and rightsizing; Simulation and measurement
(scenario tests).

Owns, all new in `internal/compute`:

- `fleet_policy.go`: `Policy` with the reference numbers, `Market`,
  `HeadroomTarget`, `Capacity` arithmetic (covers, upper, lower, clamp,
  percent rounding up).
- `fleet_catalog.go`: catalog types (shape, GPU model and count, hibernates,
  regions sold), the reviewed on-demand table per region with its review
  date, gp3 GiB-month and IPv4 hourly rates, root and swap size rules.
- `fleet_offers.go`: `RankOffers`, complete and holding cost, purchase margin
  against `billing.RateCard`, region cooling from cooldown rows.
- `fleet_cover.go`: `Cover`.
- `fleet_forecast.go`: `Forecast`, `ActivationEstimate`, expansion of
  scheduled invocations into container arrivals.
- `fleet_plan.go`: `PlanFleet` and its snapshot, plan and action types.
- Tests beside them, including `fleet_scenarios_test.go` with
  `testdata/fleet/` (a recorded Spot price snapshot and scenario inputs).

Migration: none. Protobuf and OpenAPI: none.

Depends on: nothing. The types are the contract the planner packet consumes;
keep them as plan.md describes and record any change here.

## Plan

1. Policy and capacity types with the reference defaults
   (fleet_policy.py:33-136). Unit tests for targets: floor versus load
   percent, forecast raise, Spot stopped raise to the largest running load,
   hibernation target rule, GPU markets without floors.
2. Catalog and prices. Port the reference catalog (instance_catalog.py:83-551)
   and table (supplier_prices.py:22-224), keep the rewrite's m7i.large,
   m7i.xlarge, c7i.2xlarge and r7i.2xlarge with their prices, and mark
   hibernation per type. Region lists follow what EC2 sells (us-west-1 sells
   few GPU types). A test fails when a type lacks a price where it is sold.
   Usable capacity follows the agent's rule (agent/capacity.go:93-96) on the
   nominal shape, with memory at 94% of nominal until a host of that shape
   has reported, then the reported memory of that shape
   (container_requests.py:69-76, fleet_reserves.py:42-50). A test pins the
   usable capacity of every catalog type.
3. `RankOffers` with the filters and order in plan.md. Margin uses the rate
   card in force at `now`; read `internal/billing/ratecard.go` and use its
   exported rates. If revenue for a shape needs a billing helper, add the
   smallest exported function in a separate `Propose:` commit.
4. `Cover`: one beam search (64 states per depth, depth at most the action
   cap) over items, aggregate and fit shapes, with the cost function as a
   parameter. Port the reference tests: lower total cost wins
   (test_fleet_reserve_plan.py:68), large request fits one host (:81),
   per-container packing cost (:214), partial plans report unplaceable shapes
   (:227).
5. `Forecast` and `ActivationEstimate`, porting test_demand_forecast.py,
   test_scheduled_forecast.py (scheduled part only) and
   test_activation_timing.py.
6. `PlanFleet` in the order plan.md lists: targets, demand, growth,
   retention and return to reserve, stopped growth and retirement,
   consolidation, rightsize, refresh. Port every case in
   test_fleet_reserve_plan.py and test_fleet_reserves.py as Go table tests.
   Each action names a host or an offer; nothing is a pool count.
7. Scenario tests: quiet day, burst, scheduled burst, reserve depletion, GPU
   burst, and an 8-16 vCPU burst. Each reports hourly spend, idle reserve
   spend, requests waiting over 30 s, p95 capacity wait, launches and stops,
   using fixed lifecycle timings. Print a table with `-v`; assert only
   invariants (no request waits when a ready reserve fits; spend at zero load
   equals the floors' cost).

Proposed differences P2 (quota input), P3 (stale reserve resume) and P4
(on-demand stopped reserve for Spot) land here only if the user approves;
keep each behind a separate commit so it can be dropped.

## Progress

Branch `fleet-policy` from `fleet-capacity-plan` at
08acf1dbcf9d4483d075f62d40b9739fcdc5109b. All seven plan steps are done,
plus P2 in its own commit (f35324ec) and one `Propose:` commit in billing
(4bcce9b4).

Parity lines marked "Packet: policy", with the tests that deliver them:

- Markets, headroom in capacity, warm and stopped floors, GPU shares:
  `TestFleetMarketsAreSpotOnDemandAndTheReservedCards`,
  `TestFleetTargetsKeepTheFloorOrAShareOfLoad`.
- Forecast raises targets: `TestFleetForecastRaisesTargetsAndTheReserveIsTheRest`.
- Spot reserve fits the largest running Spot host:
  `TestSpotReserveTakesTheLargestRunningSpotHostsWork`.
- Hibernation target: `TestHibernationTargetIsTheReserveWhenEveryRecentShapeFits`.
- A refused hibernation keeps its slot: `TestPlanBuysOneHibernatingReserveAndWaitsForIt`.
- Quiet market releases the largest idle host, loaded the smallest:
  `TestPlanQuietMarketsReleaseTheLargestIdleHostFirstAndLoadedOnesTheSmallest`.
- Demand forecast, 32 shapes, scheduled invocations:
  `TestForecastUsesOnlyRecentArrivalsAndKeepsPending`,
  `TestForecastHorizonsCountPendingAndTheBurstOnce`,
  `TestForecastBoundsShapesWithoutHidingOneAndRoundsUp`,
  `TestScheduledDemandUsesItsHorizonWithoutBecomingAnArrivalRate`,
  `TestShortJobsAndSeparateSchedulesUseOccupancy`,
  `TestScheduledRunsShareConcurrencyAndWarmContainers`.
- Activation horizons: `TestFragmentedReservesCannotShortenALargeRequestsForecast`,
  `TestResumeEstimatesTakeColdBootsFromMatchingHardware`,
  `TestHeldPreparationsDoNotSetServingTiming`,
  `TestActivationFailuresFallBackToProvisionAndPlainStopsBoot`.
- Location demand: `TestPlanBuysCapacityForLocationDemandAndProtectsItsHosts`,
  `TestPlanDoesNotResumeAReserveTheLocationCannotUse`.
- GPU model choice: `TestAnyGPUDemandGoesToAReservedCardTheFleetHoldsThenTheCheapestPerCard`,
  `TestGPUWorkFallsThroughToTheModelItListsNext`.
- Resume before buying, current release only, borrowing:
  `TestPlanPlacesDemandOnReadyRoomThenStartingHostsThenReservesThenPurchases`,
  `TestPlanResumesOnlyAReserveReadyForTheCurrentAgent`,
  `TestSpotWorkBorrowsAnOnDemandReserveOnlyAboveItsTarget`.
- Bounded packing and deficit cover: `TestCoverChoosesTheLowerTotalCostForRequiredCapacity`,
  `TestCoverFitsALargeRequestOnOneHostDespiteAggregateRoom`,
  `TestCoverCountsEachContainerFittingANode`, `TestCoverReportsWhatNoOfferCanPlace`,
  `TestCoverStopsAtItsNodeBoundAndReturnsTheBestPartial`,
  `TestCoverPacksTwoSixCPURequestsOntoOneLargerHost`,
  `TestPlanCoversABatchOfContainersTogether`,
  `TestPlanBuysTheLowerTotalCostForTheWarmTarget`,
  `TestPlanFitsALargeRecentShapeOnOneHostDespiteAggregateRoom`.
- 16 actions per market and pass: `TestPlanCapsGrowthPerMarketAndPass`.
- Demand and recovery hold elective growth:
  `TestPlanKeepsElectiveGrowthOutOfDemandAndRecovery`.
- Pending launches: `TestPlanPendingCapacityNeverJustifiesRetiringReadyCapacity`.
- Purchase margin: `TestPurchaseMarginKeepsThirtyPercentOfRateCardRevenue`.
- Shortfall, unmet shapes, reason: `TestPlanReportsARecentShapeNoOfferFits`.
- Catalog, regions, on-demand table, hibernation flags, usable capacity:
  `TestFleetCatalogPricesMatchTheAWSPriceList`,
  `TestFleetCatalogHibernatesOnlyCPUTypesUnderTheRAMLimit`,
  `TestFleetCatalogUsableMemoryIsWhatHostsReport`.
- Offers, ranking, exact fill, Spot use, interruption and zone, complete
  cost, region cooling, zone spreading:
  `TestOffersNeverIncludeANodeTheRequestWouldExactlyFill`,
  `TestOffersRespectInterruptionToleranceAndZone`,
  `TestOffersRankTheAuthorsGPUOrderBeforeACheaperCard`,
  `TestOfferCostIsComputeRootDiskAndPublicIPv4`,
  `TestRefusalsCoolTheOfferAndTwoInARegionMoveBuyingToTheNext`,
  `TestOffersPreferTheRegionOrderThenTheEmptierZone`.
- Return to reserve, refresh, reserve retirement, bought reserves:
  `TestPlanReturnsALeavingHostToTheReserveWhileTheReserveFallsShort`,
  `TestPlanRefreshesAStaleReserveTheTargetNeedsAndRetiresOneItDoesNot`,
  `TestPlanRetiresAPendingReserveBeforeTheReadyOneItWouldReplace`,
  `TestPlanAResumedReserveCannotCoverRetiringTheLastReadyOne`,
  `TestPlanKeepsVerifiedHibernationOverAPlainStop`,
  `TestPlanCountsAnUnverifiedHibernationAsReadyButNotSaved`,
  `TestPlanCountsEachHostOnceAndWarmRoomOnlyFromServingHosts`,
  `TestPlanDrainsAOneTimeSpotHostInsteadOfStoppingIt`,
  `TestPlanDrainsAHostThatCannotHibernateWhenTheReserveShouldHibernate`.
- Region cooling with location demand:
  `TestLocationDemandStillBuysInACoolingRegionNothingElseServes`.
- Retention, consolidation, rightsizing, pinned work:
  `TestPlanKeepsTheOnlyHostThatFitsRecentRequests`,
  `TestPlanKeepsIdleHostsUntilBilledAndLightLongEnough`,
  `TestPlanConsolidatesOnlyMovableWorkAndKeepsTheWarmTarget`,
  `TestPlanConsolidationKeepsItsDestination`,
  `TestPlanConsolidatesOneHostAtATimeAndWaitsOutTheCooldown`,
  `TestPlanRightsizesAnIdleHostAndKeepsItUntilTheReplacementServes`.
- Scenarios: `TestFleetScenarios`, `TestFleetSpendAtZeroLoadIsTheFloorsCost`,
  `TestFleetQuotaScenario`.
- P2: `TestOffersSkipTypesAKnownVCPUQuotaCannotHold`,
  `TestAQuotaRefusalCoolsTheWholeClassInItsRegionAndMarket`,
  `TestCoverStaysWithinTheQuotaRoomAcrossNodes`,
  `TestPlanCountsRunningHostsAgainstQuotasButNotStoppedReserves`.

Contract notes for the planner and provider packets:

- `FleetHost.State` uses `FleetState`; fleet_plan.go adds `preparing`,
  `stopping`, `stopped`, `hibernate_unverified` and `image_saved`, so the
  api-web packet must not declare them again. A host bought for reserve, or
  refreshing, counts as `preparing` from `requested` until it stops; a
  serving purchase in `requested` to `joining`, or a `resuming` host, is
  `starting`.
- `FleetHost.Stoppable` is set for on-demand hosts and persistent-request
  Spot reserves; a one-time Spot host only drains.
- `ReserveMode` (`stop`, `hibernate`) is declared in fleet_plan.go for the
  provider's `reserve_mode` column.
- `OfferCooldown.Quota` marks a quota refusal; the provider's refusal
  mapping sets it. `OfferInputs.Quotas` holds what Service Quotas last
  reported; a quota never read is no limit.
- `CatalogTypeNamed`, `CatalogType.RootGiB` and `Hibernates` are what the
  launcher needs for hibernation options and root size.
- `ReportedMemory` is keyed by instance type and holds the memory hosts of
  that type advertise (hosts.memory_bytes), not MemTotal.
- PlanFleet does not cool the offer of a bought host that joined and still
  cannot take its container; capacity_controller.go does that today and
  the planner keeps it.

## Intentional differences

- Plans are actions naming hosts and offers, not per-pool counts: there are
  no pools.
- One covering search serves demand packing and reserve deficits; the
  reference had two (capacity_acquisition.py, fleet_policy.py:948-1051). It
  keeps 128 states per depth for both, the reference's deficit width; the
  reference packed demand with 64. Demand items are grouped by shape, so a
  2,000-container batch costs what a handful of shapes cost.
- Backlog demand is not forecast here; it arrives as pending containers.
- Usable capacity follows the rewrite's agent (the larger of 500m or 10% of
  CPU, 512 MiB or 10% of memory, memory at 94% of nominal until reported)
  instead of the reference's divide by 1.10 and 125% memory reservation.
  Margin revenue uses the same usable numbers, so it is a little higher than
  the reference for the same host.
- Spot work borrows an on-demand reserve against the stopped target this
  pass computes, not the last published one. Demand and targets are planned
  in one pass, so there is no "without a plan" case.
- Pending request shapes join the forecast's shapes before the 32-shape
  bound, so the bound always holds; the reference added them after.
- A leaving host returns to the reserve only if it can stop the way the
  market wants: a market with a hibernation target drains a host launched
  without hibernation and buys a hibernating reserve, rather than holding
  both and retiring the plain one a pass later.
- Region cooling counts distinct type and market pairs refused in the
  window, from cooldown rows; the reference counted pool units.
- Two covers of equal cost keep the one with fewer hosts.
- Location demand matches region and zone only. The rewrite has one
  architecture (amd64) and one runtime, so architecture and runtime need no
  matching.
- "A provider that may not purchase holds no reserves" has no case: the
  platform fleet without networks gets no offers, and connection hosts
  never enter the plan.
- The 1-second billing quantum is a billing fact; the planner uses only the
  60-second minimum.
- Market reasons are more specific: waiting for demand or recovery, growth
  continues next pass (action cap), fleet host limit, no approved offer.
- Catalog regions follow AWS's price list: us-west-1 sells no g5, g6, g6e,
  p4d, p4de or p5.4xlarge, and us-east-2 no p4de. The reference listed every
  type in every region and relied on the price table to drop them.

## Evidence

- `go test -race -count=1 -run 'Fleet|Cover|Forecast|Rank|Offer|Plan|Purchase|Refusal|Spot|Hibernat|Scheduled|Activation|Resume|Fragmented|HeldPrep|ShortJobs|Quota|AnyGPU' ./internal/compute`
  passes; `./check.sh` passes; golangci-lint reports 0 issues.
- On-demand prices: every catalog price matches AWS's public EC2 price list
  published 2026-09-25 (testdata/fleet/on_demand_prices.json), including the
  reference's 2026-09-18 table. us-west-1 prices for m7i.large (117,600),
  m7i.xlarge (235,200), c7i.2xlarge (445,200) and r7i.2xlarge (588,000) µ$/h
  replace the rewrite's flat 15%.
- Spot snapshot: testdata/fleet/spot_prices.json, 158 regional Linux quotes
  from AWS's public Spot price page data, fetched 2026-10-02. It has no g6e,
  p4de, p5.4xlarge or p5en prices and no per-zone prices; the scenarios
  apply each regional price to every zone.
- At the 2026-09-10 rate card the 30% margin refuses on-demand g5.xlarge,
  g6.xlarge, g6e.xlarge, g6e.2xlarge, p5.4xlarge, p5.48xlarge and
  p5en.48xlarge, and every on-demand CPU host for Spot-tolerant work (its
  lower rate). A 1-GPU A10G or L4 request therefore buys a 2xlarge, and
  Spot-tolerant CPU work buys only Spot. The reference refused the same.
- Floors at zero load: m7i.xlarge Spot and on-demand warm spares plus
  c6a.2xlarge Spot and on-demand stopped reserves, $0.326/h ($238/month).
- Scenarios (`go test -v -run 'FleetScenarios|FleetQuotaScenario'`), one
  zone set in us-east-2, provision 300 s, resume 30 s, boot 120 s:

  | Scenario | Policy | $/h | waits > 30 s | p95 wait | launches |
  | --- | --- | --- | --- | --- | --- |
  | quiet day (24 h) | reference | 0.328 | 0/0 | 0 | 4 |
  | quiet day | demand only (today) | 0.000 | 0/0 | 0 | 0 |
  | burst, 40 x 1 vCPU | reference | 0.926 | 31/40 | 5m | 14 |
  | burst | today | 0.446 | 40/40 | 5m | 6 |
  | scheduled burst | reference | 2.174 | 0/40 | 0 | 15 |
  | scheduled burst | today | 0.446 | 40/40 | 5m | 6 |
  | reserve depletion, 3 x 12 x 2 vCPU | reference | 1.325 | 8/36 | 5m | 19 |
  | reserve depletion | today | 0.456 | 12/36 | 5m | 6 |
  | GPU burst, 4 x T4 | reference | 1.138 | 4/4 | 5m | 14 |
  | GPU burst | today | 0.461 | 4/4 | 5m | 4 |
  | 5 x 8 and 3 x 16 vCPU | reference | 1.739 | 8/8 | 5m | 16 |
  | 5 x 8 and 3 x 16 vCPU | today | 1.058 | 8/8 | 5m | 5 |
  | Oregon G Spot quota 0 (1 h) | quota unknown | 0.374 | n/a | n/a | 60 refused |
  | Oregon G Spot quota 0 | P2 | 0.374 | n/a | n/a | 0 refused |

  The reference floors absorb about ten small containers at once; a GPU or
  8 vCPU burst still waits a full provision because no floor holds those
  shapes. After a burst the forecast keeps buying for the observed rate for
  up to 10 minutes, which is most of the extra spend. No pass bought for a
  container that a ready reserve fit.

## Gaps and unverified boundaries

- The `default-test` role (lazycloud-default-test-operator in 534742592531)
  is denied ec2:DescribeInstanceTypeOfferings, ec2:DescribeInstanceTypes,
  ec2:DescribeSpotPriceHistory, servicequotas:GetServiceQuota and
  pricing:GetProducts. `TestRealEC2SellsTheCatalogWhereItIsPriced` is
  written and fails on the first call. Unverified until the role allows
  them: per-zone offerings, the hibernation flags (taken from the reference
  and, for m7i.large, m7i.xlarge, c7i.2xlarge and r7i.2xlarge, from AWS's
  documentation), and per-zone Spot prices.
- The Spot snapshot is regional public page data, not
  DescribeSpotPriceHistory. Rerun the acceptance test with
  `LAZYCLOUD_FLEET_SPOT_SNAPSHOT=testdata/fleet/spot_prices.json` once the
  role allows it.
- No AWS resources were created.

## Verification

- `go test -race ./internal/compute -run 'Fleet|Cover|Forecast|Rank|Scenario'`,
  gofmt, go vet, `go tool golangci-lint run ./internal/compute/...`,
  `./check.sh`.
- No test mocks a boundary: these are pure functions over values.
- Read-only real AWS in `AWS_PROFILE=default-test`, after
  `aws sts get-caller-identity --profile default-test` confirms the
  disposable account: `DescribeInstanceTypeOfferings` per region (and per
  zone) to confirm where each catalog type sells, `DescribeInstanceTypes`
  with `hibernation-supported` to confirm the hibernation flags, and one
  `DescribeSpotPriceHistory` per region to record the scenario snapshot.
  Launch nothing. Gate the test on `LAZYCLOUD_EC2_ACCEPTANCE_PROFILE`, as
  `ec2_acceptance_test.go` does.

## Brief

```text
You own the policy packet of the fleet capacity work for LazyCloud (repo
github.com/AmbientWare/lazycloud). Work alone; do not start sub-agents.
Create branch `fleet-policy` from origin/fleet-capacity-plan and record its
head SHA in tasks/fleet/policy.md.

Read first: AGENTS.md, tasks/fleet/README.md, tasks/fleet/plan.md,
tasks/fleet/parity.md, tasks/fleet/policy.md. The reference is commit
9e259ce75 in this repo: read it with `git show 9e259ce75:<path>` for
behavior, never for structure, and never read its env or credentials. Start
with packages/compute/src/compute/fleet_policy.py, capacity_acquisition.py,
demand_forecast.py, activation_timing.py, purchase_policy.py, offers.py,
packages/providers/aws/src/provider_aws/instance_catalog.py and
supplier_prices.py, and packages/compute/tests/test_fleet_reserve_plan.py.

Goal: deliver the parity lines marked "Packet: policy" one to one as pure
Go functions, implemented better: small functions over one snapshot, typed
actions naming hosts and offers, no pool counts. The bar is the same or
better outcome; record each difference in tasks/fleet/policy.md. Keep the
reference's numbers. Proposals P2-P4 only if the integrator says the user
approved them.

You own the new files listed under "Owns" in tasks/fleet/policy.md. Stay off
offers.go, capacity_controller.go, retirement.go, launcher.go, every
queries/*.sql, migrations/, contracts/, internal/agent, internal/api and
web/. Other agents edit those. A needed change elsewhere is a separate
commit titled `Propose: ...`, explained in your report.

Environment: go.mod pins go1.27.1 (export GOTOOLCHAIN=go1.27.1 if the
system Go is older); tools run as `go tool <name>`. You need no database.

Verify: go test -race on internal/compute with your tests, gofmt, go vet,
golangci-lint, ./check.sh. The read-only AWS checks in
tasks/fleet/policy.md under Verification, in AWS_PROFILE=default-test only,
after confirming the account with sts get-caller-identity; launch nothing.
Commit the Spot price snapshot you record.

Commit and push after each meaningful step, one-line subjects, no trailers.
Don't open PRs. Report in under 500 words: parity lines delivered with test
names, scenario results, proposed shared changes, gaps.
```
