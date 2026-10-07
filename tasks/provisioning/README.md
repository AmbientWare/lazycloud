# Provisioning

Plan branch `provisioning-plan`, from main at 6df4133ff (after #529). It never
merges as is; the final PR removes `tasks/provisioning/`.

## Decisions (2026-10-07)

- Refactor, no patches: the warm refill, the largest-shape reserve kept apart
  and the region order are replaced, not tuned. (user)
- Region choice comes from availability and full cost, not an order or a
  band: EC2 Fleet with price-capacity-optimized for launches, and offers that
  price cross-region transfer to the us-east-1 registry and storage. (user)
- Builds stay on fleet nodes; their shape feeds the warm slots. (user)
- Consolidation is phase 2, after phase 1 is measured in prod. (user)
- Prices and the rate card do not change; a per-core cost check against
  $0.044 is reported to the user separately. (user)
- Integrator defaults the user may overrule: batch window 1 s quiet, 5 s at
  most; a build slot is warm while builds ran in the market within the last
  hour.

## Packets

| Packet | Branch | Owns | Depends on |
| --- | --- | --- | --- |
| spike-fleet | prov-spike (scratch, never merges) | scratch code, EC2 in default | none |
| A planner core | prov-planner | fleet_plan.go, fleet_policy.go, fleet_cover.go, planner*.go, the planner loop in cmd/scheduler | none |
| B launch and cost | prov-launch | launcher.go, reserve_actuator.go, aws.go, fleet_offers.go, fleet_catalog.go, offers.go, zone/spot price reads, images.go build reservation | spike-fleet |

Shared contract, owned by the integrator: A decides what to buy or resume
from ranked offers; B decides what an offer is (its acceptable types and
zones, its full cost) and how it launches. `RankOffers`, `Offer` and the
buy and resume actions keep their call shape; B may add fields, A reads
only `Offer.HourlyMicros`, `Offer.StoppedMicros`, capacity and market.

## Waves

1. spike-fleet and A in parallel.
2. B, refined by the spike's answers.
3. Integration review, the prod gate, one PR, one Ship with a fleet
   replacement.

## Agent rules and gate

As PLANNING.md. No sub-agents. Real EC2 in `AWS_PROFILE=default`, US regions
only, tagged `lazycloud:task=<packet>`, removed before the report. Never touch
prod. Each packet passes ./check.sh, focused race tests, an independent review
and green CI before it merges into the plan branch.

Prod gate before the PR to main merges: python and torch cold and warm starts
against the reference platform, the torch and devbox builds, a burst of five
small starts, and a build after idle. Match or beat v0.1.99 within run-to-run
noise: python cold 1.16 s, torch cold 3.27 s, warm p50 about 100 ms, torch
build 52 s; and a burst of small starts leaves at most two hosts.
