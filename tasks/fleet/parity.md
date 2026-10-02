# Fleet capacity parity

Every fleet capacity rule and capability of the reference at `9e259ce75`.
Read a reference file with `git show 9e259ce75:<path>`. Each line names the
packet that delivers it and what the rewrite does today (`Now`). After the
sweep each line reads `Delivered: <tests>`, `Intentional: <reason>` or
`GAP: <size>`, as in the rewrite's parity file. 89 lines.

Path prefixes:
- `C` = packages/compute/src/compute
- `CT` = packages/compute/tests
- `P` = packages/providers/aws/src/provider_aws
- `A` = packages/agent/src/agent
- `S` = packages/scheduler/src/scheduler
- `W` = apps/web/src

Packets: policy, provider, agent-resume, planner, api-web, acceptance
(tasks/fleet/<packet>.md).

## Markets and targets

- [ ] Markets: Spot CPU, on-demand CPU, and on-demand per GPU card T4, A10G, L4; Spot GPU work keeps no reserve (C/fleet_policy.py:99-106, 124-136; C/fleet_resources.py:70-88)
  Packet: policy. Now: missing.
- [ ] Headroom is CPU, memory and GPUs per market, never a machine count (C/fleet_policy.py:1-9, 44-51)
  Packet: policy. Now: different, `LAZYCLOUD_FLEET_HEADROOM` idle hosts per market (retirement.go:49-60), 0 in prod.
- [ ] Spot and on-demand CPU running free room: at least 2 vCPU/4 GiB or 25% of load (C/fleet_policy.py:63-82)
  Packet: policy. Now: missing.
- [ ] Spot and on-demand CPU stopped reserve: at least 6 vCPU/12 GiB or 50% of load (C/fleet_policy.py:63-82)
  Packet: policy. Now: missing.
- [ ] GPU T4, A10G, L4 on-demand: 25% running and 50% stopped of load, no floor; other cards keep none (C/fleet_policy.py:83-106, 124-129)
  Packet: policy. Now: missing.
- [ ] Forecast raises targets: warm to the forecast's warm, total to the forecast's total, stopped = total - warm (C/fleet_policy.py:380-385)
  Packet: policy. Now: missing.
- [ ] A Spot CPU stopped reserve fits the largest running Spot host's load, so an interrupted host's work has somewhere to go (C/fleet_policy.py:386-389)
  Packet: policy. Now: missing.
- [ ] Hibernation target equals the stopped target for CPU markets when every recent shape fits a hibernation-capable shape; GPU reserves never hibernate (C/fleet_policy.py:390-407)
  Packet: policy. Now: missing.
- [ ] A refused hibernation that fell back to a plain stop keeps its hibernation slot until demand uses it (C/fleet_policy.py:1209-1232)
  Packet: policy. Now: missing.
- [ ] Quiet market (load within the warm floor) releases its largest idle host first; a loaded one its smallest (C/fleet_policy.py:408, 1072-1083)
  Packet: policy. Now: different, oldest idle hosts are kept.
- [ ] A provider that may not purchase holds no reserves and its capacity is not headroom (C/fleet_policy.py:152-154, 469-479)
  Packet: policy. Now: n/a (no reserves).

## Forecast and timing

- [ ] Demand forecast: occupancy over 60 s and 600 s windows (larger wins) plus pending plus the largest request, over a warm horizon (resume + 60 s) and total horizon (provision + 60 s) (C/demand_forecast.py:12-14, 59-178; C/fleet_policy.py:116-122)
  Packet: policy (pure), planner (inputs). Now: missing.
- [ ] Up to 32 request shapes per market; overflow merges into one covering shape (C/demand_forecast.py:186-198)
  Packet: policy. Now: missing.
- [ ] Scheduled invocations inside the provision horizon count as future demand, sharing concurrency and keep-warm containers (C/scheduled_forecast.py:56-107; packages/database/src/database/repositories/fleet_demand.py:65)
  Packet: policy (pure), planner (query). Now: missing.
- [ ] Backlog becomes container demand (C/scheduled_forecast.py:17-34)
  Packet: planner. Now: present another way; execution.Plan creates pending containers for backlog. Intentional.
- [ ] Activation horizons: p95 per kind and hardware from at least 20 samples, else defaults resume 30 s, stopped boot 120 s, provision 300 s; failures fall back to provision, cold boots to stopped boot; the fastest reserve class that covers the forecast sets the warm horizon (C/activation_timing.py:28-169; C/fleet_policy.py:91-93)
  Packet: policy (pure), agent-resume (samples), planner (aggregate). Now: missing.
- [ ] Location demands (region, zone, architecture, runtime) need compatible capacity; hosts covering them are protected (C/fleet_policy.py:807-879; C/reserve_planning.py:442-481)
  Packet: policy. Now: partial, purchases honor region and zone.
- [ ] "any" GPU demand goes to the card with units or reserves, then the cheapest per card (C/reserve_planning.py:385-414)
  Packet: policy. Now: different, GPU rank then price per offer.

## Demand coverage and purchase

- [ ] Resume ready stopped reserves before buying; placement-compatible reserves first, then cheapest (C/fleet_policy.py:481-535; C/fleet_reserves.py:329-355)
  Packet: policy, planner. Now: missing.
- [ ] Only a stopped reserve prepared for the current release replaces a purchase (C/fleet_reserves.py:258-266; CT/test_fleet_reserve_plan.py:94)
  Packet: policy. Now: missing.
- [ ] Spot work resumes an on-demand reserve only while the on-demand reserves left behind still meet their published target; without a plan, none are borrowed (C/fleet_reserves.py:270-308; CT/test_fleet_reserves.py:24)
  Packet: policy. Now: missing.
- [ ] Pending requests are covered by a bounded cost-minimizing packing (64 states); every request fits one node; unplaceable shapes are reported (C/capacity_acquisition.py:36-127; CT/test_fleet_reserve_plan.py:214, 227)
  Packet: policy. Now: different, greedy cheapest-per-container among 12 offers (capacity_controller.go:178-195).
- [ ] Warm and reserve deficits are covered by bounded combinations (128 states) priced over a one-hour horizon: running hourly x 3600 s; reserve stopped hourly x 3600 s + hourly x 300 s (C/fleet_policy.py:948-1051; CT/test_fleet_reserve_plan.py:68)
  Packet: policy. Now: missing.
- [ ] A large request must fit one host despite aggregate free room (CT/test_fleet_reserve_plan.py:81, 135)
  Packet: policy. Now: present for demand (fitFirst); missing for headroom.
- [ ] At most 16 growth actions per market per pass; the rest wait for the next pass (C/fleet_policy.py:95-96, 509, 561)
  Packet: policy. Now: different, no per-pass cap besides MaxHosts.
- [ ] Unmet demand and interruption recovery keep elective reserve growth out of their way (C/fleet_policy.py:410; CT/test_fleet_reserve_plan.py:159)
  Packet: policy. Now: n/a.
- [ ] Pending launches prevent duplicate purchases but never justify retiring serving capacity (C/fleet_policy.py:312-316; CT/test_fleet_reserve_plan.py:168)
  Packet: policy. Now: present for duplicates (InFlightHosts).
- [ ] Purchase margin: supplier cost at most 70% of rate-card revenue for the usable resources; unknown cost or unpriced capacity refuses the purchase; Spot-tolerant work keeps its lower rate on an on-demand host (C/purchase_policy.py:36-82; C/fleet_policy.py:90; CT/test_purchase_policy.py:10, 45, 76)
  Packet: policy. Now: missing.
- [ ] Each market reports its shortfall, unmet shapes and a reason (C/fleet_policy.py:736-781)
  Packet: policy, api-web. Now: reason only when no network.

## Offers, catalog and prices

- [ ] Catalog: c6a, m6a, r6a 2xlarge-8xlarge, c6i.8xlarge, m7i 2xlarge-16xlarge; g4dn, g5, g6, g6e from xlarge to 48xlarge or metal, with 1, 4 and 8 GPU sizes; p4d, p4de, p5.4xlarge, p5.48xlarge, p5en.48xlarge (P/instance_catalog.py:83-551)
  Packet: policy. Now: different, 19 types, no c6a/m6a/r6a, no single-GPU sizes above xlarge (offers.go:114-137).
- [ ] Each type records hibernation support; EC2 hibernates only under 150 GiB RAM, verified per region (P/instance_catalog.py:15, 47-66)
  Packet: policy. Now: missing.
- [ ] Offers: every catalog type in every allowed region and market it sells in (P/instance_catalog.py:552-560)
  Packet: policy. Now: present (Regions list per type).
- [ ] Region preference us-east-2, us-west-1, us-east-1, us-west-2 (C/aws_configuration.py:14-15; C/providers.py:190-196)
  Packet: policy. Now: present (offers.go regionOrder).
- [ ] On-demand prices: reviewed per-region table dated 2026-09-18, with gp3 GiB-month and public IPv4 rates (P/supplier_prices.py:7-224)
  Packet: policy. Now: different, us-east-1 rates plus a flat 15% for us-west-1 (offers.go:246-255).
- [ ] Spot prices: latest DescribeSpotPriceHistory quote per type and availability zone, reused for 60 s (P/spot_prices.py:81-205)
  Packet: provider (fetch), policy (use). Now: different, 0.4 x on-demand estimate (offers.go:139-141).
- [ ] Complete hourly cost = compute + root disk + public IPv4; stopped cost = root disk; billing minimum 60 s, quantum 1 s (P/pooled_provider.py:230-262)
  Packet: policy. Now: compute only.
- [ ] Ranking: the author's GPU order beats a cheaper card; an unknown price never outranks a known one (C/offers.py:281-305; CT/test_offer_selection.py:116, 132)
  Packet: policy. Now: present for GPU order; unknown prices impossible today.
- [ ] A node the request would exactly fill is not offered (node overhead) (shared/container_requests.py:28-41; CT/test_offer_selection.py:32)
  Packet: policy. Now: present (Offer.usable).
- [ ] Planned capacity uses the memory hosts of that shape reported once one has, nominal until then (shared/container_requests.py:69-76; C/fleet_reserves.py:42-50; C/reserve_planning.py:252-270)
  Packet: policy (rule), planner (reported memory query). Now: different, a fixed 94% of nominal (offers.go:162-166).
- [ ] Purchases respect interruption tolerance and zone (CT/test_offer_selection.py:54)
  Packet: policy. Now: present.
- [ ] A capacity refusal cools its offer for the 10-minute registration timeout (C/fleet_reserves.py:53-69; C/pool_provider.py:424-431)
  Packet: provider. Now: present (capacity_cooldowns, 10 min).
- [ ] Two refusals from offers of one region within 30 minutes rank that region after the others; it is still used when nothing else serves (C/offers.py:16-22, 323-337; C/reserve_planning.py:694-715)
  Packet: policy, provider (refused_at). Now: different, per-offer cooldown only.
- [ ] Zone spreading: the zone with fewer hosts first (C/reserve_planning.py:716-739)
  Packet: policy. Now: different, subnet by host id byte (offers.go subnetFor).
- [ ] Launch refusal codes map to capacity, quota or launch failure (P/retained_pool.py:68-80, 497-536)
  Packet: provider. Now: present (aws.go capacityRefusal).

## Reserve lifecycle

- [ ] Phases include preparing, stopping, stopped and resuming; a resumed host joins before ready; repeated heartbeats keep the phase clock (C/machine_lifecycle.py:26-107, 183-227)
  Packet: provider (table), agent-resume (session). Now: schema allows stopping, stopped, resuming; nothing uses them.
- [ ] Stop instead of terminate while the market's stopped reserve without the host falls short of target; a used host returns only after its drain (C/reserve_machines.py:476-622)
  Packet: policy (decision), planner (intent), provider (stop). Now: missing; reconcile fails and terminates any stopped instance (retirement.go:241, 254-256).
- [ ] Hibernate CPU reserves on types that support it; stop plainly otherwise (C/fleet_policy.py:390-407; C/reserve_machines.py:247-249)
  Packet: provider. Now: missing.
- [ ] Before a stop the agent proves the current agent release (C/reserve_machines.py:180-184, 280-301)
  Packet: agent-resume. Now: missing.
- [ ] A GPU reserve proves its driver before it stops: enrollment found the cards and preflight passed (C/reserve_machines.py:289-299)
  Packet: agent-resume. Now: missing.
- [ ] A hibernating reserve stops with its runtime up; a plain reserve stops with no workload (C/reserve_machines.py:264-272)
  Packet: agent-resume. Now: missing. Intentional: the rewrite has no worker process; a hibernated host keeps the agent, Docker and pulled images.
- [ ] A used host returning to reserve has no live containers and acknowledges cleanup for that exact stop request (C/reserve_machines.py:227-236, 273-279, 415-443)
  Packet: agent-resume. Now: missing.
- [ ] No agent update may be in flight on a host that stops (C/reserve_machines.py:325-329)
  Packet: agent-resume. Now: missing.
- [ ] Sleep attempts are fenced by boot id; an unaccepted attempt without a marker for 2 minutes is superseded (C/sleep_lifecycle.py:58-102)
  Packet: agent-resume. Now: missing.
- [ ] A resumed reserve takes no work until its own session authorizes the resume; a provider status lagging the boot is handled (C/reserve_machines.py:237-260, 339-359; C/machine_lifecycle.py:99-137)
  Packet: agent-resume. Now: missing.
- [ ] The resume outcome is recorded: memory restored or cold boot (C/sleep_lifecycle.py:23-55)
  Packet: agent-resume. Now: missing.
- [ ] A stopped reserve from an older release is resumed, prepared again and stopped, recording the release (C/reserve_machines.py:699-858; CT/test_reserve_machines.py:414)
  Packet: policy (Refresh action), planner, agent-resume. Now: missing.
- [ ] Surplus reserves retire, non-growable and most expensive first, keeping ready, hibernated and shape coverage; a pending reserve cannot stand in for a ready one (C/fleet_policy.py:1107-1206; C/reserve_planning.py:762-866; CT/test_fleet_reserve_plan.py:239, 263, 295)
  Packet: policy. Now: missing.
- [ ] A host bought for reserve launches, proves itself and stops before it counts as ready reserve (C/reserve_planning.py:598-621, 868-891; CT/test_fleet_reserve_plan.py:344)
  Packet: policy, planner, provider. Now: missing.

## Retention, consolidation and rightsizing

- [ ] Idle retention: hosts with containers, protected hosts, hosts before their billing minimum and hosts lightly used for under 600 s stay; idle hosts leave only while the surplus over the warm target covers them and recent shapes still fit elsewhere (C/fleet_policy.py:448-465, 1054-1104; C/fleet_reserves.py:253-257)
  Packet: policy. Now: different, drain after 5 idle minutes beyond the headroom count.
- [ ] The only host that fits recent requests is kept (CT/test_fleet_reserve_plan.py:116)
  Packet: policy. Now: missing.
- [ ] Discretionary retirement waits for a current plan; a missing or expired plan is not a zero target (C/reserve_machines.py:543-552; C/reserve_state.py:30, 48; CT/test_reserve_state.py:33)
  Packet: planner. Now: n/a.
- [ ] Consolidation: a host at or under 30% of CPU, memory and GPU for 600 s whose work all accepts interruption, with a same-placement destination that fits it, drains; the market keeps its warm target; one at a time; 900 s cooldown; given up after 3,600 s; most expensive and least loaded first (C/fleet_policy.py:111-114, 1235-1320; S/reserves.py:96-176; CT/test_fleet_reserve_plan.py:186, 397)
  Packet: policy, planner. Now: missing.
- [ ] Rightsizing: replace an idle host with a cheaper offer when hourly saving x 3600 s exceeds the new host's cost x 300 s; the source stays until the replacement serves (C/fleet_policy.py:716-735, 892-945; CT/test_fleet_reserve_plan.py:422)
  Packet: policy, planner. Now: missing.
- [ ] Pinned work (did not accept interruption, or bound to one host) never moves (packages/database/src/database/repositories/orchestration.py:778-785)
  Packet: policy. Now: n/a.

## Provider (EC2)

- [ ] Reserve launch able to hibernate: `HibernationOptions.Configured`, encrypted root, root volume plus RAM-size swap (P/managed_pool.py:1547-1548; P/platform_pool.py:63-76; P/instance_catalog.py:57-60)
  Packet: provider. Now: missing (launcher.go:106-130, 100 GiB root).
- [ ] Spot reserve on a persistent request with interruption behavior stop or hibernate, the request tagged (P/managed_pool.py:1549-1568)
  Packet: provider. Now: different, one-time request, terminate (launcher.go:137-145).
- [ ] Hibernate not within 2 minutes of start; refusals retry for 10 minutes then stop plainly; `UnsupportedHibernationConfiguration` stops plainly at once (P/retained_pool.py:51-52, 82-89, 612-733)
  Packet: provider. Now: missing.
- [ ] A stop still pending after 10 minutes is forced (P/retained_pool.py:636-656)
  Packet: provider. Now: missing.
- [ ] StartInstances refused for capacity retires the reserve and records a capacity failure (P/retained_pool.py:594-610)
  Packet: provider. Now: missing.
- [ ] Cancel a persistent Spot request before terminating its instance; verify the request launched that instance (P/retained_pool.py:851-893)
  Packet: provider. Now: missing.
- [ ] Hibernation image evidence from console output after the agent's marker: saved, failed or unknown; checks every 30 s, at most 12 per host and 2 per pass (P/retained_pool.py:54-56, 233-256, 748-829)
  Packet: provider. Now: missing. P1 proposes the agent's resume report instead.
- [ ] Launch is idempotent by client token; an unresolved launch keeps its budget for 10 minutes (P/retained_pool.py:473-495)
  Packet: provider. Now: present (ClientToken = host id, lease, 5 attempts).

## Agent

- [ ] Detect sleep from the CLOCK_BOOTTIME minus CLOCK_MONOTONIC gap over 1 s, woken by a clock-jump notice; the agent fails to start without that notice on Linux (A/suspend.py:23, 33-47, 94-185)
  Packet: agent-resume. Now: missing.
- [ ] After a sleep, rearm the interruption shutdown timer from the wall clock (A/capacity_shutdown.py:43-47)
  Packet: agent-resume. Now: missing.
- [ ] Report the sleep observation with its attempt and boot id on the next session (C/sleep_lifecycle.py:23-55)
  Packet: agent-resume. Now: missing (Hello carries boot_id only).

## Node image

- [ ] Image carries ec2-hibinit-agent and acpid, resumes by PARTUUID, and writes the smallest hibernation image (deploy/ami/bake.py:886-897)
  Packet: provider. Now: missing (deploy/ami/node-setup.sh).

## Coordination

- [ ] Plan every 60 s, or after 20 s when running free room stays short for 5 s (C/fleet_policy.py:33-34, 108-109; C/reserve_planning.py:129-152)
  Packet: planner. Now: different, capacity pass on wake and fleet tick, demand only.
- [ ] One planner at a time under a fleet lease (C/reserve_planning.py:105-127)
  Packet: planner. Now: present (TryCapacityLock).
- [ ] The plan is published for every replica and the admin page with a 5-minute expiry (C/reserve_state.py:30-60, 135-162; CT/test_reserve_state.py:14)
  Packet: planner. Now: computed per request in the API.
- [ ] One log line per changed market decision, not per pass (C/reserve_planning.py:503-529)
  Packet: planner. Now: per host requested.

## Admin API and dashboard

- [ ] `GET /v1/fleet`: per market warm free and target, reserve ready and target, allocated, counts and capacity by state, reason; plan generated and expiry times; agent release rollout (C/fleet_status.py:31-78; apps/api/src/api/server/routers/resource_api/fleet.py:17-25)
  Packet: api-web. Now: reserve ready and target are always zero (internal/api/fleet.go:54-55); warm target is idle hosts' capacity.
- [ ] `GET /v1/fleet/nodes`: paged platform nodes with region, type, market, GPU, state (including preparing, stopping, stopped, hibernate_unverified, image_saved), capacity, allocated, containers, ready (C/fleet_status.py:80-141)
  Packet: api-web. Now: no reserve states are produced.
- [ ] Provider-refused launches never appear in Nodes (P/retained_pool.py:507-536, 1072-1073; C/fleet_status.py:96-110)
  Packet: api-web. Now: regression, listed as Failed for a day (launcher.go:150-151, 240; queries/fleet_admin.sql:17-18).
- [ ] Dashboard Fleet: Capacity tab with expandable market states and targets; Nodes tab with infinite scroll; expired plan message; Refresh (W/components/shared/SettingsDialog/AdminSettings/FleetSettings.tsx:24-299)
  Packet: api-web. Now: present, same look; data missing.

## Simulation and measurement

- [ ] Offline scenarios (quiet, burst, scheduled burst, activation failure, reserve depletion) report capacity misses, cost, packing, churn and startup delays (C/simulation.py:161-660; packages/compute/README.md)
  Packet: policy (scenario tests), acceptance (comparisons). Now: missing. Intentional: Go scenario tests, no CLI; rollout scenarios dropped with maintenance.

## Not rebuilt

- [ ] Auto Scaling groups, launch templates and pool count reconciliation (P/managed_pool.py; C/unit_reconciliation.py)
  Intentional: per-host RunInstances; recorded in tasks/fleet/plan.md.
- [ ] Capacity maintenance rollouts with reserved replacements (C/maintenance.py; C/maintenance_policy.py)
  Intentional: agents update in place.
- [ ] Redis plan state (C/reserve_state.py)
  Intentional: PostgreSQL `fleet_markets`.
- [ ] Reserves in connected accounts (C/reserve_machines.py:500)
  Intentional: the reference kept reserves for the platform fleet only.
