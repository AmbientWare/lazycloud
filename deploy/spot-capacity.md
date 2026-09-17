# Operate platform Spot capacity

The platform uses the cluster-owned Spot NodePool in
`deploy/platform-core/node_capacity.tf`. Auto Mode selects instance types
and node counts from pod resource requests. Use the
[runbook](RUNBOOK.md#spot-capacity-and-replica-placement) for inspection commands.

## Preserve availability during changes

The API, scheduler, Cloudflare connector, and connection gateway use replicas
spread across hosts and zones. Check both running revisions during a rollout.
Disruption budgets limit voluntary drain; they cannot prevent provider reclamation.

Argo and External Secrets also use Spot nodes. Auto Mode's provisioning
controllers run outside those nodes, so replacement provisioning does not
depend on Argo remaining available.

The cache has one ReadWriteOnce EBS volume. A replacement must attach that same
volume in its zone. Do not increase replicas against the claim or discard the
volume to get a pod running.

## Review capacity

Read requests from the chart, Argo, and External Secrets together. Compare
them with node allocatable CPU and memory, then measure real usage and rollout
headroom. Do not lower requests to force a desired node count.

For a pending pod, inspect scheduling events, NodeClaims, NodeClass conditions,
and provider availability in the same cycle. If progress stops, investigate the
reported constraint before draining another node.

## Move an existing pool

1. Record the current nodes, workload placement, volume identities, and costs.
2. Provision replacement capacity through the canonical Terraform owner,
   preserving network and IAM identities.
3. Apply the workload placement rules through Argo.
4. Drain only the selected old nodes, one at a time, while checking workload
   outcomes and replacement capacity.
5. Disable the previous pool only after replacement provisioning and workload
   recovery pass.

A failed apply can leave resources outside Terraform state. Reconcile exact
provider IDs and ownership before another apply or cleanup.

## Verify recovery and cost

Measure equivalent traffic before and after the change. Verify task completion,
retry ownership, gateway reconnection, cache reads after replacement, Argo
reconciliation, and secret refresh. Record any evidence unavailable for abrupt
host loss or a broad Spot shortage.

Calculate savings from the actual selected nodes, prices, Auto Mode charges,
disks, IPs, transfer, and replacement overlap. A quoted Spot price or a projected
monthly layout is not an observed bill.

## Recover from failed qualification

If availability or savings fail the reviewed bounds, restore the approved
On-Demand capacity through its owning configuration, verify it can serve work,
then drain only the selected Spot nodes. Preserve cache data, credentials,
network identities, and unrelated resources. Use the managed EKS API if Argo
is unavailable.
