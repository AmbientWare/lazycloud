# Platform Spot plan

Status: proposed, no infrastructure changes applied. Evidence collected on
2026-09-10 UTC with AWS profile `default` against the `lazycloud` cluster.

Use one On-Demand node and two Spot nodes, retaining the existing application
replica counts. This is the best supported cost/availability tradeoff for the
current requests and architecture. Qualify it only if measured net savings are
at least $50 per 730-hour month and single-node recovery passes. This $50 floor
is the proposed threshold for making the operational work worthwhile.

## Cost decision

The live cluster has three `c6a.large` On-Demand nodes. Each has a public IPv4
address, a 4 GiB root disk and an 80 GiB data disk. The separate 20 GiB cache
volume is unchanged by this proposal. AWS Pricing API rates in `us-east-1` are
$0.0765/hour for the instance, $0.00918/hour for its EKS Auto Mode management,
$0.005/hour for IPv4, and $0.08/GiB-month for gp3 storage.

At 730 hours, the current node fleet costs approximately **$218.75/month**:
`3 × [730 × (0.0765 + 0.00918 + 0.005) + 84 × 0.08]`.
The estimate excludes unchanged services and variable traffic and replacement
costs. Auto Mode management charges do not receive the Spot discount.
[AWS EKS pricing](https://aws.amazon.com/eks/pricing/)

| Steady fleet | Node cost/month | Savings/month |
| --- | ---: | ---: |
| Three On-Demand, current | $218.75 | $0 |
| Two On-Demand, one Spot | $184.80 to $192.10 | $26.64 to $33.94 |
| One On-Demand, two Spot, proposed | $150.86 to $165.46 | $53.29 to $67.89 |
| Two On-Demand, two Spot | $223.78 to $238.38 | Costs $5.03 to $19.63 more |
| Three Spot | $116.91 to $138.81 | $79.94 to $101.84 |

These scenarios use `c6a.large` equivalents, unchanged disks, and Spot rates
of $0.03 to $0.04/hour. They are sensitivity calculations, not guaranteed quotes
for every eligible instance family. Actual Auto Mode fees must follow the
instance type selected.

EC2's seven-day Spot history for `c6a.large` showed $0.0265 to $0.0336/hour across
zones `us-east-1a`, `1b`, and `1c`. Latest prices were $0.0279, $0.0336, and
$0.0307 respectively. The latest `1a` plus `1c` pair gives a $149.84/month mixed
fleet, saving $68.91 before other cost changes. Price history proves price,
not available capacity. Also sampled `c6i`, `c7a`, `c7i`, `m6a`, `m6i`, `m7a`,
and `m7i` large instances to establish alternatives.

Cost Explorer's September 3 through 8 usage corroborated the instance and
management rates. Account-wide costs include other compute and services and
must not be used as a platform-only baseline. September data remains estimated,
and September 9 was incomplete when queried. Check active commitments before
rollout so moving instances cannot strand prepaid On-Demand spend.

Spot leaves the $73/month EKS cluster fee, approximately $23.36/month Redis pair,
load balancer, cache volume, database, and other services in place. Do not
advertise the node percentage saving as a percentage of the whole bill.

Allow $10/month provisionally for additional cross-zone traffic and replacement
overlap. The latest-price case still saves about $59/month; the $0.04 Spot case
would miss the $50 net target. Recalculate using actual selected instances and
usage before accepting the migration. Spot cannot guarantee a permanent saving.

## Placement and availability

Keep two API replicas, two schedulers, two Cloudflare connectors, two WireGuard
gateways, and one cache server. Retain `wireguard.platformPeers: 2` and existing
database budgets. Adding replicas is not part of the saving.

Keep Argo, External Secrets and the cache on On-Demand. Distribute the API,
Cloudflare and WireGuard pairs with one replica on each capacity type. Place
the two schedulers on distinct Spot nodes in distinct zones. Both schedulers
already coordinate through shared Redis locks; their production implementation
does not change.

The following packing fits current effective pod requests, including API
sidecars and the gateway's larger init-container memory request:

| Node | Workloads | CPU request | Memory request |
| --- | --- | ---: | ---: |
| On-Demand | Argo, External Secrets, cache, one API, connector and gateway | 1,450m | 2,880 MiB |
| Spot A | One API, scheduler, connector and gateway | 1,500m | 2,144 MiB |
| Spot B | One scheduler | 750m | 640 MiB |

Each current node advertises 1,780m CPU and about 3,065 MiB memory allocatable.
This is a feasibility calculation, not an observed placement after migration.
The On-Demand node has only about 185 MiB of request headroom. Keep bootstrap
jobs on eligible Spot capacity and verify deployments, reconnect bursts, pod
limits and system overhead before accepting this packing. Do not reduce
resource requests to force the arithmetic to fit. Two current-size nodes
cannot hold the existing total 3,700m request against 3,560m allocatable.

Enforce separation across hosts, zones, and capacity types where specified.
Use workload-wide selectors across rollout revisions, rather than treating
each revision as a separate group. Use `minDomains: 2` with hard spread where
two independent domains are required. Check the combined constraints during
startup, rollout and replacement; server-side validation alone cannot prove
that pods will schedule.
[Kubernetes topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)

The cache volume is bound to `us-east-1c`; preserve it and schedule its
On-Demand replacement there. Offer Spot capacity across all three existing
subnets, preferring three-zone node placement without requiring an extra node.
Require at least two zones for each replicated service. Leave the 80 GiB data
disks intact: one current node uses about 32 GiB already, so a speculative disk
reduction is not included in the saving.

A single node loss leaves an API, connector, gateway and scheduler running.
WireGuard still has one active lease holder, so a gateway loss can interrupt
connections while the standby takes over. Losing both Spot nodes leaves the
On-Demand API/ingress path but pauses scheduling until Spot capacity returns.
This is an explicit limit of the proposal. Three Spot nodes save more but leave
the entire platform exposed to a Spot capacity shortage. Disruption budgets
limit voluntary disruption; they do not stop AWS reclaiming capacity.
[Kubernetes disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)

## Implementation sequence

1. Capture a comparable baseline before changing placement. Reuse existing
   history and collect only missing evidence. Record seven days
   of node-hours by type, Auto Mode charges, node disks/IPs, regional traffic,
   NLB usage, API errors/latency, reconnects, scheduler queue delay, memory and
   CPU under normal load and a rollout. Use existing telemetry and kubelet
   resource metrics; the cluster currently has no Metrics API. Attribute costs
   to the exact cluster resources and exclude customer fleet and other account
   usage. Keep secret values out of evidence.

2. Add one cluster-owned Spot NodePool, reconciled by an Argo Application under
   `deploy/argocd/apps/`, referencing a manifest under `deploy/platform-compute/`.
   Reuse the existing Auto Mode `default` NodeClass, node identity and network.
   Retain the built-in On-Demand `general-purpose` pool. This avoids creating
   another network, node controller or storage configuration. Update
   `deploy/platform-core/AGENTS.md` and its runbook to describe the explicit
   Spot pool policy; preserve the `CLAUDE.md` symlink.

3. Configure Spot-only capacity, `amd64`, and a diverse selection of supported
   C/M instance families. Do not pin a single cheapest instance or add GPU,
   ARM, burstable or exotic instances without a production need. Let Auto Mode
   size nodes from requests. Set resource limits for bounded replacement
   headroom and a one-node voluntary disruption budget. Resource limits are
   not a dollar cap or a guarantee of exactly three nodes. EKS Auto Mode handles
   interruption notices itself.
   [AWS NodePools](https://docs.aws.amazon.com/eks/latest/userguide/create-node-pool.html),
   [Auto Mode lifecycle](https://docs.aws.amazon.com/eks/latest/userguide/automode.html)

4. Implement the placement policy in `deploy/chart/values.yaml`,
   `values.schema.json`, the workload templates and `_helpers.tpl`. Place Argo
   through `deploy/platform-core/argocd.tf` and External Secrets through its
   existing Argo Application. Retain one workload controller per service.
   Use capacity-type affinity and spread to place pairs, not duplicate service
   deployments or a second runtime implementation. Specify bootstrap-job
   placement so it cannot silently create a permanent On-Demand node floor.
   Update `deploy/chart/README.md` and `deploy/RUNBOOK.md` together.

5. Run the existing deployment-definition validation for the changed owners:
   Terraform validation, production values rendering, Helm lint/render, and
   Kubernetes server-side dry runs for the proposed resources. Then exercise
   real placement and a rollout. Do not add tests of generated manifest shape.
   Use a feature branch and PR; merge only after applicable checks pass.

6. Migrate serially after reviewing the exact live plan. Provision the Spot
   pool first, retain healthy serving replicas, and drain one selected old node
   at a time. Preserve the cache PVC and all existing secrets, identities and
   deployment pins. Record each replacement's identity and incremental cost.
   The steady fleet must settle at three nodes; brief rollout/replacement surge
   is measured, not counted as permanent capacity. Budget at most $5 for the
   migration and acceptance overlap, then stop and reassess if exceeded.

## Acceptance and rollback

Prove continued API service, correct durable task outcomes, scheduler lease
recovery, worker reconnection and WireGuard failover during a controlled drain
and process/pod loss. Poll records, both ends' logs, readiness, placement and
provider state every cycle. Investigate a stalled cycle immediately. Do not
claim that a voluntary drain proves abrupt EC2 loss or Spot replacement under
capacity scarcity. EKS Auto Mode does not support FIS EC2 Spot-interruption or
termination actions; use supported pod experiments and record the remaining
host-interruption evidence gap explicitly.
[Auto Mode experiment limitations](https://docs.aws.amazon.com/eks/latest/userguide/automode-learn-instances.html)

Keep the change only after a comparable seven-day observation passes all of:

- Three steady nodes with the specified independent replicas, no persistent
  Pending pods, OOMs or CPU starvation, and successful normal rollouts.
- No durable task loss, duplicate terminal settlement or broken cleanup.
  Meet existing production SLOs. In their absence, proposed qualification
  bounds are steady-load API p95 latency and placement delay within 10% of
  baseline, error rate increasing by no more than 0.1 percentage points, and
  gateway connectivity recovering within 60 seconds of losing the active pod.
  Record equivalent request volume and task mix when comparing the windows.
- At least $50/month net savings at equal workload, including actual instance
  prices, management fees, disks, IPs, extra traffic, replacement overlap and
  any commitment effects. With a $5 migration budget, payback is under four
  days at that saving. Recheck billing completeness before making the decision;
  do not treat a partial day's charges as savings.
- A stressed price calculation still passes, using seven-day upper observed
  prices for the selected families and zones. Otherwise keep observing or
  reject the migration rather than assuming today's low prices persist.

Review cost and interruption rates weekly afterward. If the saving disappears
or availability fails the agreed bounds, restore the previous Git placement
policy, provision sufficient On-Demand capacity, then drain only the named
Spot nodes. Keep the cache volume and shared resources. Remove the now-unused
Spot pool through its owning Argo Application only after its workloads have
moved. This is an operator rollback, not an automatic On-Demand fallback that
can silently defeat the cost target.
