# Provisioning plan

## Goal

Buy and keep capacity the way demand actually arrives: pack small starts onto
fewer hosts, start builds warm or on a resume, choose regions by availability
and full cost, and report holds truthfully. Same or better start latency.

## Evidence (prod v0.1.99, 2026-10-07)

- Five 1-CPU on-demand starts made five m7i.large hosts. Each used the
  market's 1 CPU of warm headroom; each refill bought the cheapest cover
  (fleet_plan.go:819-845). A stopped 16-CPU reserve sat unused because warm
  growth only resumes reserves that leave a large one behind (830, 675-680).
- The largest-shape reserve is kept apart (fleet_policy.go:207); the warm path
  never uses it and reserveFor ranks it last (fleet_plan.go:670).
- Builds reserve 4 CPUs and 2 GiB but may use 8 GiB (images.go:46-49), so they
  oversubscribe neighbours. After idle they resume a reserve (13 s).
- The Spot reserve logs "no approved offer" while the floor is deliberately
  held (fleet_plan.go:971, 999-1020, 1160-1170).
- Price alone picks the region (fleet_offers.go:293-301); the fleet went to
  us-west-2 while the registry and storage are in us-east-1, and builds paid
  about 2 s more to push.

## Design

Planner core (A):
- One cover pass packs pending work and warm slots together. Warm slots are
  shapes of recent starts: one small slot always, plus the build shape while
  builds ran recently. A burst packs onto one host; the separate warm refill
  path goes.
- One reserve model: the same pass chooses stopped and hibernated reserves
  and decides resume against buy by cost and wait. No reserve is kept apart;
  reserve shapes come from the shapes that start.
- A batch window before buying (1 s quiet, 5 s at most). Resumes and starts
  that fit ready capacity never wait.
- Hold reasons name the hold; real exclusions name themselves.

Launch and cost (B):
- Launches go through EC2 CreateFleet (type instant) with
  price-capacity-optimized over a short list of acceptable types and zones
  per decision, replacing single-type RunInstances.
- Offers carry their full hourly cost: compute, root disk throughput, IPv4,
  and expected cross-region transfer to us-east-1 (registry pushes and pulls,
  layer and volume reads). regionOrder() goes.
- Builds reserve the memory they may use.

## How Karpenter does it

Provisioner batches pending pods (batcher.go:81-118), packs them largest first
into existing, in-flight and new NodeClaims whose instance type set narrows as
pods join (scheduler.go:611-646, nodeclaim.go:264-270), keeps headroom as
virtual pods packed with real ones (provisioner.go:224-228), and launches
through EC2 Fleet with price-capacity-optimized. Consolidation (phase 2)
follows disruption/consolidation.go with budgets, do-not-disrupt and
replace-before-delete.

## Open questions (the spike answers them)

1. Does CreateFleet type instant launch a Spot instance with hibernation
   configured and a persistent request, so a stopped reserve resumes as
   today? Or does persistence need type request or maintain?
2. Launch latency: CreateFleet instant vs RunInstances, same type and zone,
   five runs each.
3. Does a Fleet launch return a usable error per pool (capacity, quota, price)
   so cooldowns still work?
4. Cost of GetSpotPlacementScores calls, and whether price-capacity-optimized
   alone is enough.
