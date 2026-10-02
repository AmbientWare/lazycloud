# Fleet capacity: reserves, resume, offers and sizing

## Same product, better implementation

The reference is github.com/AmbientWare/lazycloud at `9e259ce75`. Read it with
`git show 9e259ce75:<path>`; never run it or read its env. It is the product
spec for platform fleet capacity: every rule in tasks/fleet/parity.md ships
with the same outcome or better, implemented cleanly in Go under AGENTS.md.
The rewrite dropped this system (`git show d6afb1df^:tasks/compute.md`, lines
19-22 and 205-209) and runs a demand-only fleet: buy when a container fits
nowhere, drain after 5 idle minutes, keep `LAZYCLOUD_FLEET_HEADROOM` idle
hosts per market (0 in prod).

This plan keeps the original's concepts and policy numbers. Where a rule's
behavior would change, it is listed under "Proposed differences" with its
reason, and the original rule stays the default until the user approves.

## What the original meant to do

Keep enough capacity of the right shape that a request lands on a ready host,
and keep the rest of the reserve as stopped machines that cost only their
disk.

- Each purchase market (Spot CPU, on-demand CPU, on-demand T4, A10G and L4)
  keeps two targets in CPU, memory and GPUs, not machine counts. Running free
  room is at least 2 vCPU/4 GiB or 25% of load. Stopped reserve is at least
  6 vCPU/12 GiB or 50% of load. GPU markets keep 25% and 50% with no floor.
  A demand forecast can raise both.
- Stopped CPU reserves hibernate where the instance type supports it, so a
  resume restores memory in about 30 s instead of booting in about 120 s or
  launching in about 300 s.
- Demand resumes a ready stopped reserve before it buys anything. Spot work
  may borrow an on-demand reserve only while the on-demand reserves left
  behind still meet their target.
- Purchases pick the cheapest bounded combination of instance types that fits
  every request on one host, across regions in preference order, refusing
  offers under a 30% margin or with unknown cost. A capacity refusal cools
  the offer for 10 minutes; two in one region within 30 minutes move buying
  to the next region.
- An idle host leaves only when the targets and recent request shapes no
  longer need it. If the stopped reserve is short, it stops into the reserve
  instead of terminating. Lightly used hosts are consolidated, and expensive
  idle hosts are swapped for cheaper ones when that pays back within an hour.
- The admin Fleet page shows each market's running free room and reserve
  against their targets, and every node's state including stopped and
  hibernated.

## Why the original was complex

About 19,000 Python lines across compute, the AWS provider and the scheduler
loops served fleet capacity, including the pools it shared with connected
accounts. The causes become rules here.

- Planning was in counts per pool ("unit"): `desired`, `stopped`,
  `retiring_stopped`, `retained`. A reconciler converged an Auto Scaling group
  or retained pool toward the counts, so the planner had to reason about which
  slot the provider might remove ("pool targets do not name the retired slot",
  fleet_policy.py:1174). Rule: plans name hosts and offers directly.
- `_plan_market` (fleet_policy.py:338-804) is one 470-line function that
  repeats the same per-unit "ready minus retiring" expression in seven places.
  Rule: small pure functions over one snapshot, each with one output.
- Hot plan state lived in Redis with TTLs (reserve_state.py:1-7), PostgreSQL
  held the rest, and every action re-read and re-checked both. Rule: one
  PostgreSQL authority, NOTIFY to wake, typed rows for the published plan.
- Sleep evidence had three stores: sleep attempts, activations and pool slot
  JSON (sleep_lifecycle.py, capacity_activations, retained_pool.py:91-123).
  Rule: a host's sleep and resume facts are columns on the host.
- Demand acquisition and reserve planning ran as separate services that
  blocked each other through a "demand" market set (fleet_policy.py:410).
  Rule: one pass covers demand first, then reserves, in one transaction.
- A provider protocol abstracted one provider (providers.py:264-343) and a
  maintenance subsystem rolled agent releases with reserved replacements
  (maintenance.py, 882 lines). The rewrite updates agents in place. Rule: one
  AWS implementation, no maintenance rollouts.

## Gap audit, verified

The earlier audit's nine findings hold, with one correction: the 16-action
cap applies per market per pass, not per pass (fleet_policy.py:95-96, 509).
It missed these:

- Reconcile fails and terminates any stopped or stopping instance
  (retirement.go:241, 254-256), so nothing can be held stopped today.
- Spot launches are one-time with terminate (launcher.go:137-145); a Spot
  reserve needs a persistent request, and terminating it must cancel the
  request first.
- Launches set no hibernation options and a fixed 100 GiB root
  (launcher.go:26, 106-130); hibernation needs room for a RAM-size swap file.
- The catalog has 19 types (offers.go:114-137): none of the reference's
  c6a, m6a or r6a types, which were the cheapest per vCPU, and no single-GPU
  size above xlarge, so a 1-GPU request needing 8 vCPU buys a 4-GPU host.
- Prices are us-east-1 on-demand rates with a flat 15% for us-west-1 and
  Spot estimated at 0.4 of on-demand (offers.go:139-147, 246-255). No disk
  or IPv4 cost, no Spot price history, no 30% purchase margin check, and Spot
  zones are chosen by host id rather than price (offers.go subnetFor).
- Purchases are greedy per container among the 12 cheapest offers
  (capacity_controller.go:178-195), while the reference searched packings of
  the whole batch. Compute simulates first fit on ready hosts and scheduling
  places best fit (scheduling/placement.go:172-198); a mismatch costs at
  most one extra pass.
- Planned memory is a fixed 94% of nominal; the reference switched to what
  hosts of that shape reported (container_requests.py:69-76).
- No demand forecast and no scheduled-invocation forecast, so a cron burst
  is never prepared for.
- The agent has no sleep detection (internal/agent); after a resume it would
  wait for gRPC keepalive to notice the dead session.
- The node image has no hibinit-agent, acpid or resume setup
  (deploy/ami/node-setup.sh). The reference's zram swap is also absent; that
  concerns container memory ceilings, not reserves, and is out of scope here.
- The platform IAM role lacks Start, Stop, GetConsoleOutput,
  DescribeSpotPriceHistory and Spot request actions
  (deploy/terraform/platform-deployment/iam.tf:82-170).
- `LiveCloudHostCounts` counts every non-failed AWS host against `MaxHosts`
  (queries/fleet.sql:33-38), so stopped reserves would use up launch room.
- `PendingDemand` uses `array(select jsonb_array_elements_text(...))`, an
  ARRAY(subquery) form; check it against Neki's router when the planner
  packet moves it.
- Neither version reads EC2 quotas; with the Oregon G Spot quota at 0, every
  attempt there is a refusal (P2).

Already right: region order us-east-2, us-west-1, us-east-1, us-west-2
(offers.go regionOrder), GPU preference order, per-offer 10-minute
cooldowns, client-token launches, and demand reaching compute as pending
containers that execution creates.

## Target design

### Owners

- compute owns machine capacity: offers, prices, cooldowns, the reserve plan,
  every host phase, and every EC2 call. It reads pending containers and
  schedules as demand, as it does today.
- scheduling owns placement and is unchanged: it places only on hosts that
  are `online`, `ready` and `available`, so reserves never receive work.
- execution owns containers. It creates pending containers for backlog
  (execution.Plan), which is how backlog demand reaches compute; compute's
  original backlog forecast (scheduled_forecast.py:17-34) is not needed.
  Consolidation drains through the existing `Containers.DrainHostContainers`.
- The agent (host runtime, no database) detects resume from sleep, reconnects
  at once, and proves readiness before a stop over the host protocol.

### Durable state

All in PostgreSQL. Each packet adds only its own columns and tables.

- `hosts` (existing). New phase `preparing`; `stopping`, `stopped` and
  `resuming` already pass the check constraint. New columns, by owner:
  - provider (migration 0003): `reserve_mode` (`stop`, `hibernate`, null for
    a serving host), `hibernation_configured`, `spot_request_id`,
    `node_image`, `stop_requested_at`, `force_stop_at`, `stopped_at`,
    `resume_requested_at`, `image_evidence` (`unknown`, `saved`, `failed`,
    `unavailable`), `evidence_checks`, `evidence_next_at`.
  - host protocol (migration 0004): `sleep_attempt_id`, `sleep_boot_id`,
    `prepared_agent_version`, `gpu_proven`, `last_resume_outcome`
    (`memory_restored`, `cold_boot`).
  - planner (migration 0005): `light_since`.
- `spot_prices (region, availability_zone_id, instance_type, hourly_micros,
  effective_at, observed_at)` (0003). A leader timer refreshes it.
- `capacity_cooldowns` (existing) gains `refused_at` (0003) so region cooling
  counts refusals within 30 minutes.
- `fleet_activations (id, kind, instance_type, region, gpu_type, seconds,
  outcome, at)` (0004), pruned after a day. It feeds the activation
  estimates (activation_timing.py).
- `fleet_markets (market primary key, plan jsonb, generated_at, expires_at,
  pressure_since, consolidating_host, consolidation_started_at,
  consolidation_cooldown_until)` (0005). The planner upserts one row per
  market each pass; the admin API reads it. No seed rows, so no
  same-migration reads (Neki).

The planner writes intents; the actuator performs provider calls. This is
the contract between the packets:

| Intent | Written by | Row change | Actuator does |
| --- | --- | --- | --- |
| Buy to serve | planner | insert `requested`, `reserve_mode` null | RunInstances (exists) |
| Buy for reserve | planner | insert `requested`, `reserve_mode` set | RunInstances with hibernation or persistent Spot when set |
| Return to reserve | planner | `ready` to `preparing`, `reserve_mode` set; only on-demand hosts and persistent-request Spot reserves, never a one-time Spot host | nothing; the session asks the agent |
| Agent proved readiness | host session | `preparing` to `stopping`, `sleep_attempt_id` | StopInstances, `Hibernate` when `reserve_mode = hibernate` |
| Resume to serve | planner | `stopped` to `resuming`, `resume_requested_at`, `reserve_mode` cleared | StartInstances |
| Host says Hello after a launch or resume | host session | to `joining`; on first session `ready`, or `preparing` when `reserve_mode` is set | nothing |
| Retire reserve or idle host | planner | `stopped` or `ready` to `terminating` or `draining` | cancel Spot request, TerminateInstances |
| Refresh a stale reserve | planner | `stopped` to `resuming` with `reserve_mode` kept | StartInstances; the session prepares and stops it again |

### Lifecycle state machine

One transition table in compute, checked on every phase write with a
`from_phase` guard, as `SetHostPhase` already does.

```text
requested -> provisioning -> booting -> joining -> ready
ready <-> draining -> terminating -> deleted
ready -> preparing -> stopping -> stopped -> resuming -> joining -> ready
joining -> preparing                 (reserve_mode set: bought for reserve or refreshing)
preparing -> ready                   (agent refused: work arrived; an update in flight keeps it in `preparing`)
stopping -> stopped | resuming       (a pass may resume a reserve still stopping)
stopped -> terminating               (retire reserve)
resuming -> terminating              (StartInstances refused for capacity)
any -> failed                        (provider stopped or terminated it unasked,
                                      boot timeout, lost service)
```

Rules carried from the original:

- A resumed host joins before it is ready; heartbeats do not reset
  `phase_at` (machine_lifecycle.py:60-62, 193-196).
- Only an authorized resume serves. A Hello from a `stopping` or `stopped`
  host whose resume was requested is a lagging resume and proceeds; one
  without `resume_requested_at` stays out of placement and is stopped again
  (reserve_machines.py:237-260, machine_lifecycle.py:119-137).
- EC2 refuses hibernation in the first 2 minutes after start; refusals retry
  for 10 minutes, then the host stops plainly. An instance launched without
  hibernation stops plainly at once. A stop stuck for 10 minutes is forced
  (retained_pool.py:51-52, 82-89, 612-733).
- A Spot reserve runs on a persistent request with interruption behavior
  `stop` or `hibernate`; terminating it cancels the request first, or EC2
  relaunches it (managed_pool.py:1549-1568, retained_pool.py:851-893).
- A Spot reserve whose StartInstances is refused for capacity terminates and
  cools its offer (retained_pool.py:594-610).
- A host EC2 stopped without being asked (Spot interruption, operator) fails
  as `provider_stopped` and terminates, as today (retirement.go:254-256).

### Pass structure and caps

The scheduler leader runs one fleet planning pass. It replaces
`PlanCapacity` and the idle drain in `Retire`.

- Wakes: `lc_compute` NOTIFY (pending container, host phase change), every
  60 s, and at most every 20 s while a market's running free room has been
  short of target for 5 s (fleet_policy.py:33-34, 108-109).
- One transaction under the existing `TryCapacityLock`:
  1. Read the snapshot with a fixed number of queries: platform hosts with
     load and state; up to 2,000 pending containers grouped by shape and
     placement; the last 10 minutes of platform container arrivals,
     aggregated in SQL by market and shape over a uuidv7 id range; schedules
     due within the provision horizon joined to release resources; cooldowns;
     fresh Spot prices; activation p95s; `fleet_markets`.
  2. Run the pure planner (below).
  3. Write intents, container waits and `fleet_markets` rows, then NOTIFY.
- The actuator runs after commit, outside any transaction, claiming intents
  with leases and `FOR UPDATE SKIP LOCKED`: at most 10 launches and 10
  start/stop/terminate calls per pass, and 2 console-evidence reads. Every
  call is idempotent (client token, or a state EC2 already reports).
- Caps: at most 16 growth actions per market per pass; the rest wait for the
  next pass (fleet_policy.py:95-96, 509). `MaxHosts` bounds running and
  starting hosts per owner; stopped reserves have their own bound of the
  same size, so reserves never take launch room.
- A missing or expired plan (older than 5 minutes) is not a zero target:
  retirement and Spot borrowing of on-demand reserves wait for a current
  plan (reserve_machines.py:543-552, test_reserve_state.py:33).

### Pure functions

All in `internal/compute`, no database or AWS access, deterministic in their
inputs. Capacity is `{CPUMillis, MemoryBytes, GPUs}` in usable units, the
same numbers the agent advertises.

`Forecast(samples, scheduled, pending, timings) -> MarketForecast`

- Inputs: arrivals in the last 600 s with shape, count and duration;
  scheduled arrivals within the provision horizon; pending capacity; the warm
  horizon (fastest covering reserve class + 60 s) and total horizon
  (provision + 60 s).
- Output: `Warm` and `Total` capacity (occupancy over the 60 s and 600 s
  windows, the larger, plus pending and the largest request, plus scheduled
  peaks), and up to 32 request shapes, overflow merged into one covering
  shape (demand_forecast.py:59-198).

`ActivationEstimate(samples, policy, reserves) -> seconds per kind`

- p95 per kind and hardware from at least 20 samples, else the policy
  default (resume 30 s, stopped boot 120 s, provision 300 s); any failure
  falls back to provision, any cold boot to stopped boot. The warm horizon
  uses the fastest reserve class that covers the forecast
  (activation_timing.py:28-169).

`RankOffers(need, placement, catalog, prices, cooldowns, now) -> []Offer`

- Filters: catalog type sold in the region; market allowed by the request
  (Spot only for preemptible work); not cooling; complete cost known (a Spot
  offer needs a price observed within an hour); usable capacity covers the
  need; GPU model accepted and count enough; GPU hosts only for GPU work;
  supplier cost at most 70% of the rate card's revenue for the usable
  resources (a 30% margin), with Spot-tolerant work keeping its lower rate on
  an on-demand host (purchase_policy.py:36-82).
- Order: GPU preference rank (the author's order beats a cheaper card);
  cooling regions last; complete hourly cost; region order us-east-2,
  us-west-1, us-east-1, us-west-2; fewest hosts in the zone; key.
- Complete hourly cost is compute + root disk + public IPv4; a reserve's
  holding cost is root disk only. Spot offers are per availability zone
  because the price is.

`Cover(offers, need, cost, maxNodes) -> purchases, unmet`

- One bounded beam search (64 states per depth) for both uses in the
  original (capacity_acquisition.py:36-127, fleet_policy.py:948-1051).
  `need` holds items (each placed whole on one node, consuming room),
  aggregate capacity to supply beyond the items, and shapes that must each
  fit an empty chosen node.
- `cost` is hourly x 3600 s for serving hosts, or stopped hourly x 3600 s +
  hourly x 300 s for reserves.
- A complete cover minimizes cost; otherwise the best partial cover returns
  with the unmet items and shapes named.

`PlanFleet(policy, snapshot) -> Plan`

Input snapshot: hosts (id, market, usable shape, placement, phase, load,
containers, pinned containers, protected, billing settled, ready for the
current release, reserve mode, image evidence, hourly and stopped cost,
`light_since`), pending demand groups, forecasts, ranked offers, market rows
(`pressure_since`, consolidation, cooldown), the agent release, recovering
markets, `MaxHosts` room.

Output: per market, the targets and measures the admin page shows (load,
quiet, warm target and free, warm pending, stopped target, reserve ready and
pending, hibernation target, hibernated and unverified capacity, shortfalls,
unmet shapes, reason, counts by state); a list of typed actions (`Resume`,
`Buy`, `BuyReserve`, `ReturnToReserve`, `Drain`, `RetireReserve`,
`Consolidate`, `Rightsize`, `Refresh`), each naming a host or an offer and
the containers it is for; and container waits (`provisioning`, `limit`).

Per market, in order:

1. Targets. Warm = max(floor, 25% of load) raised to the forecast's warm;
   total = stopped(load) + warm raised to the forecast's total; stopped =
   total - warm. Spot CPU raises stopped to the largest running Spot host's
   load. If every recent shape fits a hibernation-capable shape, a CPU
   market's hibernation target equals its stopped target
   (fleet_policy.py:349-407).
2. Demand. Simulate pending containers, largest first, on ready free room,
   then on hosts starting. For the rest: resume ready reserves that fit
   (placement-compatible first, then cheapest), with the borrowing rule for
   Spot work; then `Cover` over ranked offers within `MaxHosts`.
3. Growth runs only when the market has no unmet demand and is not
   recovering from an interruption (fleet_policy.py:410). Warm shortfall and
   unmet shapes resume reserves first, then buy.
4. Retention. Idle hosts leave only after 10 minutes of light use, after the
   billing minimum, and while the surplus over the warm target covers them
   and every recent shape still fits elsewhere; most expensive first, largest
   first when quiet. A leaving host returns to the reserve when the stopped
   reserve without it falls short and its type can be a reserve; otherwise
   it drains and terminates (fleet_policy.py:1054-1104,
   reserve_machines.py:580-622).
5. Stopped growth. Fill the stopped and hibernation targets by buying for
   reserve. Surplus reserves retire, non-growable and most expensive first,
   keeping ready, hibernated and shape coverage (fleet_policy.py:1107-1206).
6. Consolidation (no shortfall, market not cooling). A host at or under 30%
   of CPU, memory and GPU for 600 s whose containers all accept
   interruption, whose departure keeps the warm target, with a
   same-placement destination that fits its load, drains. One per market at a
   time; 900 s cooldown; given up after 3,600 s (fleet_policy.py:1235-1320).
7. Rightsize (nothing else growing). Replace an idle host with a cheaper
   offer that covers the need when the hourly saving x 3600 s exceeds the new
   host's cost x 300 s (fleet_policy.py:892-945).
8. Refresh. A stopped reserve prepared for an older agent release is not
   ready; it is resumed, updated in place and stopped again, counted in the
   action cap (reserve_machines.py:699-858).

### Prices and refresh

- On-demand: a reviewed per-region table in Go, with its review date, for
  every catalog type in every region that sells it, plus gp3 GiB-month and
  public IPv4 hourly rates. Start from the reference table dated
  2026-09-18 (supplier_prices.py) and add the types the rewrite added. A test
  fails when a catalog type lacks a price in a region it is offered in.
- Spot: `DescribeSpotPriceHistory` with `StartTime = now` per availability
  zone, per region, for catalog types, every 5 minutes on a leader timer,
  upserted into `spot_prices`. One region per call batch; a failed region
  keeps its last prices, which expire for purchase after an hour.
- No Spot estimate. The rewrite's `spotDiscount = 0.4` and flat 15% us-west-1
  premium (offers.go:139-147, 246-255) go.

### Sizing and packing

- One fit rule. Planned usable capacity applies the agent's reserve
  (agent/capacity.go:22-27, 93-96) to the nominal shape, as `Offer.usable`
  does (offers.go:162-166), and `HostCapacity.Fits` checks it. Memory starts
  at 94% of nominal and switches to what hosts of that shape reported once
  one has (container_requests.py:69-76).
- Every request fits one host; aggregate room never proves a fit
  (test_fleet_reserve_plan.py:81).
- An 8 vCPU request cannot fit an 8 vCPU host (usable 7.2 vCPU), so it gets a
  16 vCPU host. The remaining 6.4 vCPU counts as running free room toward
  the warm target, the warm spare must fit recent shapes, and placement packs
  later work into it. Today each such request buys a new host, because
  nothing keeps room of that shape and the purchase is greedy per container
  (capacity_controller.go:178-195, 305-353).
- The batch of unplaced containers is covered as a whole with `Cover`, so
  two 6 vCPU containers share one 16 vCPU host when that is cheaper.
- Multi-GPU: a request for N GPUs fits only a host with at least N accepted
  GPUs; GPU containers pack onto multi-GPU hosts when cheaper. "any" GPU
  demand is charged to the card with stock, else the cheapest per card
  (reserve_planning.py:385-414).
- The catalog returns to the reference's breadth: c6a, m6a, r6a, c6i and m7i
  CPU sizes from 2xlarge to 16xlarge, the rewrite's m7i.large and xlarge,
  and single-GPU g4dn, g5, g6 and g6e sizes up to 16xlarge beside the 4- and
  8-GPU sizes, p4d, p4de, p5.4xlarge, p5.48xlarge and p5en. Today a 1-GPU
  request needing 8 vCPU jumps to a 4-GPU host. Each type records whether it
  hibernates (RAM under 150 GiB and verified per region).

### Nodes and Capacity API

The contract already has every field (`contracts/openapi.yaml` FleetState,
FleetMarket); only descriptions change.

- `GET /v1/fleet`: `plan.generated_at` and `expires_at` from
  `fleet_markets`; per market `warm_free`, `warm_target`, `reserve_ready`,
  `reserve_target`, `allocated`, `states[]` and `reason` from the published
  plan; agent release rollout as today. An expired plan shows "Capacity data
  expired" in the dashboard, as the reference did.
- `GET /v1/fleet/nodes`: every platform host that has an instance, by id,
  paged. States: `serving`, `starting`, `draining`, `preparing`, `stopping`,
  `stopped`, `hibernate_unverified`, `image_saved`, `unavailable`, `failed`,
  `terminating`. `ready` means serving on the current agent, or for a
  reserve, prepared for the current agent. A launch the provider refused has
  no instance and is never listed; the rewrite lists it as failed for a day
  (fleet_admin.sql:17-18).
- The dashboard keeps its look; FleetSettings.tsx already renders these
  states and columns.

## Keep, drop, rebuild

Rebuild:

- Market targets, forecast, activation estimates, borrowing, resume first,
  bounded covering, margin, cooldowns and region cooling, retention,
  return-to-reserve, reserve retirement, consolidation, rightsizing, stale
  reserve refresh, plan publication with expiry, pressure-driven early pass.
- Hibernation on launch, persistent Spot reserves, hibernate with fallback,
  forced stop, Spot request cancel, console evidence, agent resume
  detection, node image hibernation support.
- The reference catalog and reviewed prices; live Spot prices.
- The admin numbers and states.

Drop, with reasons:

- Pools, Auto Scaling groups, launch templates and count reconciliation.
  Per-host `RunInstances` with a client token is simpler and already works.
- Capacity maintenance rollouts with reserved replacements (maintenance.py).
  Agents update in place without draining.
- Redis plan state and per-replica decision caches (reserve_state.py).
- The provider protocol and the connected-account pooled provider for
  reserves. Reserves are platform-only, as they were (`unit.platform_fleet`,
  reserve_machines.py:500).
- Backlog-to-container forecasting. Execution already creates those
  containers.
- The Python simulation CLI. Its scenarios become Go scenario tests.
- `LAZYCLOUD_FLEET_HEADROOM` (hard cut; the policy replaces it).

## Decisions taken (defaults)

1. Policy numbers are the reference's, as typed constants in one `Policy`
   value in code, reviewed like prices. The existing `MAX_HOSTS` and
   `IDLE_TIMEOUT` stay; an idle host waits for the later of `IDLE_TIMEOUT`
   and 600 s of light use, as the reference's pool timeout and retention
   did together.
2. One planning pass covers demand then reserves; actions name hosts.
3. Hibernation evidence follows the reference: console output read after the
   stop (see P1 for the simpler option).
4. Spot prices refresh every 5 minutes into PostgreSQL, not a 60 s
   in-process cache: replicas share one view and the admin page can show it.
5. Reserves stay platform-only; connected accounts keep today's fleet.
6. Root disk stays at the rewrite's 100 GiB; a hibernating host adds its RAM
   in GiB for swap.
7. Full-path acceptance (agent resumes and serves) runs on lazycloud-prod
   after one Ship of the integrated branch; provider-level acceptance runs in
   `default-test`. Instances in `default-test` cannot reach a local server.
8. Packets merge into `fleet-capacity-plan`; one PR to main and one Ship at
   the end, with tasks/fleet removed.

## Proposed differences

Each changes a rule's behavior. The original rule ships unless the user says
yes; the packet named carries the change.

- P1, provider and agent-resume. Drop console-output hibernation evidence.
  Count a hibernation as saved once EC2 accepted `Hibernate=true` and the
  instance stopped with reason `Client.UserInitiatedHibernate`; the agent's
  resume report (same boot id and slept seconds, or a new boot id) is the
  proof. A cold boot after a hibernation marks that type and region
  unreliable for a day. Reason: removes `GetConsoleOutput`, kernel log
  parsing that breaks across kernels, and up to 12 reads per host; the cost
  is one slow resume (about 90 s instead of 30 s) when a hibernation
  silently failed.
- P2, policy and provider. Read EC2 vCPU quotas per region and class
  (Service Quotas) hourly, skip offers that would exceed them, and let a
  quota refusal cool the whole class in that region. Reason: the Oregon G
  Spot quota was 0 on 2026-09-18, so each G Spot attempt there is a refusal,
  a wasted pass and a 10-minute cooldown per type.
- P3, policy. When no current reserve fits, resume a reserve prepared for an
  older agent and let it update in place after joining, instead of buying.
  Reason: an in-place update takes seconds; a launch takes minutes.
- P4, policy. Hold the Spot CPU market's stopped reserve as on-demand
  stopped instances. Reason: stopped cost is disk-only in either market, and
  a stopped Spot instance cannot start when its market is short, which is
  when the reserve matters. Decide after acceptance measures Spot start
  refusals.

## Measurements

Each with conditions, before (rewrite today) and after.

- Cold start per path and type (c6a.2xlarge, m7i.xlarge, g4dn.xlarge): launch
  to agent ready; StartInstances on a stopped reserve to ready; StartInstances
  on a hibernated reserve to ready; then first container ready on each.
  Report p50 and p95 over at least 10 runs each.
- Stop and hibernate durations versus RAM size; hibernation success rate.
- Planner pass cost with 10, 100 and 1,000 hosts and 0, 500, 2,000 and 10,000
  pending containers: wall time, statements, rows read and shared buffers per
  statement. Pass cost must stay flat beyond the 2,000-row batch; a guard test
  pins the statement count.
- Price outcomes from Go scenario tests (quiet day, burst, scheduled burst,
  reserve depletion, GPU burst) on the reviewed prices and a recorded Spot
  price snapshot: hourly spend, idle reserve spend, requests waiting over
  30 s, p95 capacity wait, launches and stops per hour. Compare the rewrite
  today, the reference policy, and each proposal.
- Idle cost at zero load. Estimate with the reference floors: about
  $147/month for an on-demand m7i.xlarge warm spare, about $60 for a Spot
  one, and about $19 for two stopped 8 vCPU reserves (116 GiB gp3 each),
  about $225 in all. Measure it.
- Admission and placement latency separately from capacity wait and user
  execution, as AGENTS.md asks.

## Open questions for the user

1. The reference floors cost about $225/month at zero load (estimate above).
   Keep them, or keep only the stopped and hibernated floors (about $19) and
   accept a 30-60 s resume for the first request after quiet? Default: keep.
2. Approve any of P1-P4? Default: none until measured; P1 and P2 are the
   ones I recommend.
3. Full-path acceptance on lazycloud-prod after one Ship, or a disposable
   stack in `default-test` behind a public TLS name? Default: prod.

## Completion

- [ ] Every line in tasks/fleet/parity.md delivered or recorded as intentional
- [ ] Measurements above recorded with before and after
- [ ] Neki-safe SQL checked in every new query
- [ ] Dead code removed: `HeadroomFloor`, `cheapestPerContainer`, `price`,
      `spotDiscount`, `regionPremium`, the old idle drain
- [ ] Final review gate passed, tasks/fleet removed, one PR, one Ship
- [ ] Full-path acceptance on prod recorded; task resources cleaned up
