# Platform Spot plan

Status: proposed; no infrastructure changes applied. Keep EKS Auto Mode.
Recommend qualifying all Kubernetes worker nodes on Spot for the owner's cost
priority, retaining application replication and the existing managed data
services. On-Demand is additional protection against a broad Spot shortage,
not a technical requirement for running this application.

This proposal accepts recovery periods after correlated Spot interruptions or
loss of singleton services. It does not promise uninterrupted service. Prove
the current application's recovery behavior before deployment acceptance.

## Architecture

Use one cluster-owned Spot-only NodePool with a broad selection of compatible
amd64 C/M/R instance families across the existing three availability-zone
subnets. Let Auto Mode select instance types, sizes and counts from accurate
pod resource requests. Do not prescribe two nodes or specific instance sizes.

Keep two replicas each of the API, scheduler, Cloudflare connector and
WireGuard gateway. Require different hosts and zones for each pair, including
during rollouts. Use workload-wide selectors and minDomains of two for the
required topology domains. Keep disruption budgets that allow one replica to
move at a time, without blocking ordinary consolidation.
[Kubernetes topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)

A Spot capacity pool is tied to an instance type and zone. Multiple zones and
eligible types reduce exposure to one pool's interruption or shortage; they do
not guarantee replacement capacity. Do not add replicas automatically or force
extra nodes merely to create a nominally larger fleet.
[AWS Spot allocation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-fleet-allocation-strategy.html)

The EKS managed control plane, PostgreSQL, managed Redis and object storage
remain outside the Spot node fleet. Auto Mode's autoscaler, load-balancer
controller and storage controller are managed off-cluster. Argo being
unavailable does not stop Auto Mode from creating replacement nodes.
[AWS Auto Mode components](https://docs.aws.amazon.com/eks/latest/best-practices/automode.html)

Argo and External Secrets also run on Spot. Their existing Kubernetes resources
and projected secrets outlive an individual worker node. Prove their restart,
reconciliation and secret refresh after node replacement; do not assume that
application replication also makes these controllers replicated.

Keep the single cache server and its existing 20 GiB EBS volume. The volume is
bound to us-east-1c, so its replacement requires capacity in that zone. Do not
increase cache replicas against the same ReadWriteOnce volume or discard the
volume. Cache unavailability and recovery are explicit acceptance scenarios.

WireGuard is active/standby, not two simultaneous forwarding paths. Test
handover and worker reconnection. Existing streams can break during node loss.
A broad Spot shortage can stop the application until replacement capacity is
available, even though the managed EKS control plane remains available.
Disruption budgets cannot prevent involuntary EC2 reclamation.
[Kubernetes disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)

## Cost evidence

Evidence was collected read-only on September 10, 2026 UTC with AWS profile
default. The current cluster has three On-Demand c6a.large nodes. AWS Pricing
API rates are $0.0765/hour for c6a.large and $0.153/hour for c6a.xlarge.
Auto Mode costs $0.00918/hour and $0.01836/hour respectively, regardless of the
EC2 purchase option. Each existing node has one public IPv4 address at
$0.005/hour and 84 GiB of gp3 disks at $0.08/GiB-month.
[AWS EKS pricing](https://aws.amazon.com/eks/pricing/)

Spot projections use seven full days of time-weighted prices ending September
10 at 04:07 UTC. Hourly c6a.large averages were $0.02698121 in us-east-1a,
$0.03220031 in 1b, and $0.02971000 in 1c. The c6a.xlarge average in 1c was
$0.06643164. These are observations, not guaranteed future prices or capacity.

| Illustrative layout | Node cost/month | Saving versus current |
| --- | ---: | ---: |
| Current: three On-Demand c6a.large | $218.75 | $0 |
| Spot c6a.xlarge in 1c and Spot c6a.large in 1a | $109.04 | $109.71 |
| Three Spot c6a.large, one per zone | $116.11 | $102.64 |
| Protected alternative: On-Demand xlarge in 1c, Spot large in 1a | $172.23 | $46.52 |

Every row uses 730 hours, unrounded price inputs, Auto Mode, node IPv4 and
unchanged 84 GiB node disks. The separate EKS cluster fee is $73/month.
Database, Redis, load balancing, the cache volume and traffic remain additional
in every case. These are costed examples, not fixed node layouts.

Allow $10/month provisionally for incremental traffic and replacement overlap.
The two all-Spot examples then save approximately $93 to $100/month net.
Require at least $80/month in measured net savings at comparable workload to
qualify this migration. That is a proposed acceptance target, not a guaranteed
bill. Recalculate for the node types and counts Auto Mode actually selects.

## Capacity accounting

The live cluster has 19 application/operator pods requesting 3,700m CPU and
5,664 MiB memory in total. This includes seven Argo pods at 500m CPU and
1,024 MiB, and three External Secrets pods at 100m CPU and 224 MiB. No Pending
pods or separately installed EKS add-ons were observed. Auto Mode supplies its
node services.

Current two-vCPU nodes expose 1,780m CPU and approximately 3,065 MiB memory
allocatable to pods. Use allocatable resources rather than raw instance
capacity. Effective pod requests include API sidecars and the gateway's larger
init-container request.

The two-node cost example can place one API, scheduler, connector and gateway
on each node. The smaller node would request 1,500m CPU and 2,144 MiB memory;
the larger, also holding Argo, External Secrets and the cache, would request
2,200m and 3,520 MiB. Verify real allocatable capacity, system overhead,
reconnect bursts and rollout headroom. This arithmetic is a feasibility check,
not observed placement after migration.

Auto Mode sizes from declared requests; it does not determine those requests
from actual application usage for us. Keep them accurate from representative
measurements. Do not lower requests to force a particular node count. Leave
the 80 GiB data disks intact: one current node already uses about 32 GiB.
[AWS Auto Mode cost optimization](https://docs.aws.amazon.com/eks/latest/userguide/auto-cost-control.html)

## Implementation sequence

1. Reuse existing history for a comparable seven-day baseline of cost, API
   errors/latency, placement delay, reconnects, resource usage and rollouts.
   Collect missing evidence only. Kubelet resource metrics are available even
   though the Metrics API is absent. Attribute costs to exact platform
   resources; account-wide billing also includes customer and other compute.

2. Add the Spot NodePool and its canonical NodeClass through one cluster-owned
   Argo Application under deploy/argocd/apps/. Reuse existing network and
   identities. Resolve NodeClass ownership and bootstrap ordering before
   disabling the built-in On-Demand pool; do not rely on an implicitly
   retained default NodeClass. Preserve the EKS cluster, access entries and
   data. Update deploy/platform-core/AGENTS.md and its runbook, keeping the
   CLAUDE.md symlink intact.
   [AWS NodePools](https://docs.aws.amazon.com/eks/latest/userguide/create-node-pool.html)

3. Update Helm values, schema, placement helpers and workload templates
   together. Keep current replica counts, database budgets and two platform
   WireGuard peers. Remove On-Demand placement requirements from this
   proposal; keep host/zone separation. Include Argo, External Secrets and
   bootstrap jobs in the migration. Preserve production ownership and one
   controller per service. Update deploy/chart/README.md and deploy/RUNBOOK.md.

4. Validate changed Terraform owners, render through the production values
   boundary, run Helm lint and Kubernetes server-side dry runs. Then prove
   real placement and a normal rollout. A valid manifest is not scheduling
   proof. Do not add tests of generated manifest shape.

5. Review the exact live migration plan and provision replacement capacity
   before draining selected old nodes. Move serially while preserving healthy
   application replicas. Retire the unused On-Demand pool only after workloads
   have moved and replacement provisioning is proven independent of it.
   Allow temporary rollout/replacement surge, not a fixed node-count ceiling.
   Record every created resource and its cost. Budget $5 for migration and
   acceptance overlap, then reassess if exceeded.

## Acceptance and rollback

Keep the change only after proving:

- Host/zone separation for every replicated service, no persistent Pending
  pods, OOMs or starvation, and successful bootstrap jobs and normal rollouts.
- Correct durable task outcomes, scheduler lease recovery, worker reconnection
  and gateway handover during controlled drain and pod/process loss.
- Cache recovery with the same PVC in its bound zone; Argo reconciliation and
  External Secrets refresh after replacement. Exercise cold startup without
  assuming the old node or its local files remain.
- No durable task loss, duplicate terminal settlement or broken cleanup.
  Meet existing production SLOs. In their absence, proposed qualification
  bounds are steady-load API p95 latency and placement delay within 10% of
  baseline, error rate increasing by at most 0.1 percentage points, and
  gateway connectivity recovering within 60 seconds of active-pod loss.
  Compare equivalent traffic and task mixes.
- At least $80/month net savings over a comparable seven-day observation,
  including actual instance prices, Auto Mode, disks, IPs, incremental traffic,
  replacement overlap and commitment effects. Recheck incomplete billing days
  and repeat the calculation with seven-day upper observed prices for the
  selected types and zones.

Poll durable records, logs at both ends, readiness, placement and provider
state every cycle. Investigate a stalled cycle immediately. A voluntary drain
does not prove abrupt EC2 loss or replacement during Spot scarcity. Auto Mode
does not support FIS EC2 Spot-interruption or termination actions. Use supported
pod experiments and record remaining host-loss evidence gaps explicitly.
[AWS experiment limitations](https://docs.aws.amazon.com/eks/latest/userguide/automode-learn-instances.html)

Review cost and interruption rates weekly. If savings or availability fail
their bounds, restore the known On-Demand pool/placement through its canonical
owner, qualify sufficient capacity, then drain only the named Spot nodes.
Preserve cache data, secrets, identities and unrelated resources. Keep a
reviewed recovery route using the managed EKS API if Argo is unavailable.
This is an operator rollback, not a silent automatic On-Demand fallback.

After acceptance, profile demonstrated scheduler CPU waste before considering
smaller resource requests. Book no extra saving until real resource use falls
and unchanged production behavior is proven.
