# Platform cost and availability plan

Status: proposed; no infrastructure changes applied. The owner selected keeping
EKS Auto Mode and wants production quality at minimum practical cost.

Use one four-vCPU On-Demand node and one two-vCPU Spot node. Keep two replicas
of the API, scheduler, Cloudflare connector and WireGuard gateway, with one
of each on each capacity type. Put Argo, External Secrets and the existing
cache on On-Demand. This keeps a complete serving and scheduling path running
when Spot capacity disappears.

The quoted target is **$172.23/month for nodes**, down from $218.75. Including
the unchanged EKS cluster fee, that is $245.23 versus $291.75. Database, Redis,
load balancing, the cache volume and traffic are additional in both cases.

## Cost evidence

Read-only evidence was collected on September 10, 2026 UTC using AWS profile
default. The live cluster has three c6a.large On-Demand nodes. AWS Pricing API
rates in us-east-1 are $0.0765/hour for c6a.large and $0.153/hour for c6a.xlarge.
Their Auto Mode fees are $0.00918 and $0.01836/hour respectively.

Every existing node has one public IPv4 address, a 4 GiB root disk and an 80 GiB
data disk. IPv4 costs $0.005/hour; gp3 costs $0.08/GiB-month at the existing
baseline IOPS and throughput. Keep these disk sizes: one current node uses
approximately 32 GiB, so shrinking them has not been qualified.

Spot estimates use time-weighted price history over seven full days ending
September 10 at 04:07 UTC. The hourly averages for c6a.large were $0.02698121
in us-east-1a, $0.03220031 in 1b, and $0.02971000 in 1c. For c6a.xlarge in 1c,
the average was $0.06643164. These are observations, not guarantees of future
prices or available capacity.

| Layout, all retaining Auto Mode | Nodes/month | Saving/month | Availability tradeoff |
| --- | ---: | ---: | --- |
| Current: three On-Demand c6a.large | $218.75 | $0 | Existing baseline |
| Proposed: On-Demand c6a.xlarge in 1c, Spot c6a.large in 1a | $172.23 | $46.52 | Complete application replica on each capacity type |
| On-Demand large in 1c, Spot large in 1a and 1b | $150.26 | $68.49 | Both schedulers depend on Spot |
| Three Spot large, one in each zone | $116.11 | $102.64 | Whole platform depends on Spot |
| Spot xlarge in 1c, Spot large in 1a | $109.04 | $109.71 | Whole platform depends on Spot |

Every row includes 730 hours, observed prices, unchanged 84 GiB node disks,
IPv4 and Auto Mode. Totals use unrounded inputs. Cost Explorer corroborated
the current instance and management rates. Account-wide charges include other
compute and must not be substituted for the platform baseline.

The proposed node bill is $111.69 On-Demand EC2, $19.70 Spot EC2, $20.10 Auto
Mode, $7.30 IPv4, and $13.44 disks. Its management fee still covers six raw
vCPUs. The EKS cluster fee is separately $73/month.
[AWS EKS pricing](https://aws.amazon.com/eks/pricing/)

Allow $10/month provisionally for changed traffic and replacement overlap.
That leaves approximately **$36.52/month net savings**, or $438/year. Verify
the allowance with actual usage. The earlier $50/month minimum was an
assistant-selected target, not an owner requirement. Replace it with a
$30/month net savings floor while preserving the complete On-Demand path.

The extra $21.97/month over the three-node mixed option keeps a scheduler
running during a complete Spot shortage. All-Spot saves more but is not the
recommendation for the owner's quality requirement.

## Placement

| Node | Workloads | Effective CPU requests | Effective memory requests |
| --- | --- | ---: | ---: |
| On-Demand, four vCPUs | Argo, External Secrets, cache, one API, scheduler, connector and gateway | 2,200m | 3,520 MiB |
| Spot, two vCPUs | One API, scheduler, connector and gateway | 1,500m | 2,144 MiB |

Requests include the API sidecar and the gateway's larger init-container
memory request. Current small nodes expose 1,780m CPU and approximately
3,065 MiB allocatable, so the Spot packing fits. Verify the larger node's
actual allocatable capacity, system overhead, and burst behavior. Its nominal
four vCPUs and 8 GiB are not a substitute for that check.

Keep two platform WireGuard peers, current database budgets and replica counts.
Do not reduce requests to force placement. Two current-size nodes cannot hold
the existing 3,700m CPU request. The proposed differently sized pair retains
six raw vCPUs.

For every replicated service, enforce different hosts, different zones, and one
replica per capacity type. Use workload-wide selectors across rollout revisions
and minDomains of two where two independent domains are required. Verify
combined constraints during startup, rollout and replacement; valid YAML does
not prove schedulability.
[Kubernetes topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)

Preserve the cache PVC, bound to us-east-1c. Its replacement On-Demand node
must be eligible there. Offer Spot across existing subnets and several
compatible families. The quoted 1a placement is a cost example, not a
permanent pin to one Spot market.

Losing either node leaves an API, scheduler, connector and gateway. Losing
the On-Demand node also stops the cache and deployment/secret controllers
until replacement. Those services already lack independent replicas. Preserve
their data and prove recovery. WireGuard remains active/standby; failover can
interrupt connections. Existing streams are not guaranteed to survive node
loss. Simultaneous loss of both nodes can still stop service.

## Implementation sequence

1. Reuse existing history for a comparable seven-day cost and service baseline.
   Collect only missing evidence for API errors/latency, placement delay,
   reconnects, CPU/memory, traffic and rollouts. The Metrics API is absent;
   kubelet resource metrics are available. Attribute costs to exact platform
   resources and exclude customer fleet usage.

2. Define two capacity policies at the cluster owner. The On-Demand policy
   needs enough allocatable capacity for 2,200m CPU and 3,520 MiB of protected
   requests, justifying a four-vCPU minimum. Spot must fit the other complete
   application replica. Keep amd64 and compatible C/M families; let Auto Mode
   choose within the cost envelope. Resource limits do not guarantee the node
   count or dollar bill.

3. Reconcile custom NodePools through one cluster-owned Argo Application under
   deploy/argocd/apps/. Reuse network and node identities. Resolve NodeClass
   ownership and bootstrap ordering before retiring any unused built-in pool;
   do not assume its default NodeClass will remain. Preserve cluster access
   and existing identities; never replace the cluster. Update
   deploy/platform-core/AGENTS.md and its runbook for the measured capacity
   policy, preserving the CLAUDE.md symlink.
   [AWS NodePools](https://docs.aws.amazon.com/eks/latest/userguide/create-node-pool.html)

4. Update Helm values, schema, placement helpers and workload templates
   together. Retain one controller per service and the production
   implementation. Place Argo through deploy/platform-core/argocd.tf and
   External Secrets through its existing Argo Application. Give bootstrap jobs
   explicit eligible placement. Update deploy/chart/README.md and
   deploy/RUNBOOK.md in the same change.

5. Validate changed Terraform owners, render through the production values
   boundary, run Helm lint, and perform Kubernetes server-side dry runs.
   Exercise real scheduling and a normal rollout to prove packing. Do not add
   tests of generated manifest shape. Work on a feature branch and merge its
   PR only after applicable checks pass.

6. Review the exact live migration plan, then replace nodes serially.
   Provision and qualify replacements before draining selected old nodes.
   Preserve serving replicas, cache PVC, secrets, deployment pins, identities
   and customer resources. Permit temporary rollout/replacement surge; do not
   impose a hard two-node ceiling. Record created resources and their cost.
   Budget $5 for migration and acceptance overlap, then reassess if exceeded.

Auto Mode keeps ownership of node replacement, interruption handling,
networking/DNS, load balancing, storage and Pod Identity integration. Do not
install duplicate controllers or an automatic On-Demand fallback for
Spot-selected replicas.
[Auto Mode lifecycle](https://docs.aws.amazon.com/eks/latest/userguide/automode.html)

## Acceptance and rollback

Keep the change only after the production implementation proves:

- One complete application replica on each capacity type, two steady nodes
  at the measured workload, and no persistent Pending pods, OOMs or CPU
  starvation. Bootstrap jobs and ordinary rollouts succeed.
- Controlled node drain and pod/process loss preserve correct task outcomes,
  scheduler lease recovery, worker reconnection and gateway failover. Poll
  durable records, logs at both ends, placement, readiness and provider state
  every cycle. Investigate stalled progress immediately.
- No durable task loss, duplicate terminal settlement or broken cleanup.
  Meet existing production SLOs. Where none exist, proposed qualification
  bounds are steady-load API p95 latency and placement delay within 10% of
  baseline, error rate increasing by at most 0.1 percentage points, and gateway
  connectivity recovering within 60 seconds of active-pod loss. Compare
  equivalent traffic and task mixes.
- At least $30/month net savings over a comparable seven-day observation,
  including actual instance rates, management fees, disks, IPs, incremental
  traffic, replacement overlap and commitment effects. Recheck incomplete
  billing days. Repeat the calculation with seven-day upper observed prices
  for selected types/zones. At the minimum saving, a $5 migration pays back
  in about five days.

A voluntary drain does not prove abrupt EC2 loss or replacement during Spot
scarcity. Auto Mode does not support FIS EC2 Spot-interruption or termination
actions. Use supported pod experiments and record remaining host-loss evidence
gaps. Do not claim uninterrupted streams or immunity to involuntary disruption.
[Auto Mode experiment limitations](https://docs.aws.amazon.com/eks/latest/userguide/automode-learn-instances.html),
[Kubernetes disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)

Review cost and interruption rates weekly. If savings disappear or availability
fails its bounds, restore the previous Git placement policy, provision enough
On-Demand capacity, then drain only the named replacement nodes. Preserve the
cache volume and shared resources. Remove unused pools through their canonical
owner after workloads have moved.

## Further savings after acceptance

Profile scheduler CPU under representative load and investigate demonstrated
redundant work. Lower requests only after reducing actual resource needs and
proving unchanged task behavior. Re-evaluate smaller nodes with those measured
requests. No additional saving is booked for this work yet. Removing replicas,
moving durable services onto Spot, and removing Auto Mode are outside this plan.
