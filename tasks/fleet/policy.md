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

## Intentional differences

- Plans are actions naming hosts and offers, not per-pool counts: there are
  no pools.
- One covering search serves demand packing and reserve deficits; the
  reference had two (capacity_acquisition.py, fleet_policy.py:948-1051).
- Backlog demand is not forecast here; it arrives as pending containers.

## Evidence

## Gaps and unverified boundaries

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
