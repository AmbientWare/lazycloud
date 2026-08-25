# LazyCloud chart

The control plane, the scheduler, the cache, the tunnel, and the bootstrap that
has to run before any of them.

Values come from the platform module rather than being authored here: `images`
from what the deploy pushed, `runtime` from `runtime_configuration`,
`secrets.map` from `secret_environment`, and `roles` from `workload_role_arns`.
Nothing in this chart decides a value the infrastructure already knows.

## The order the bootstrap runs in

Hook weights, not preference. The schema must exist before an administrator can
be created against it, and the administrator must exist before anything
authenticates.

The administrator credential is the one with a trap in it. `auth bootstrap`
adopts a configured credential when it finds one and mints its own when it does
not, recording a different bootstrap request id for each. Install without
`administrator-token` written to Secrets Manager and the credential exists only
inside that Job's pod, every later step has no bearer token, and supplying the
value afterwards is refused as an already completed bootstrap. The way back is
resetting the schema.

Write it before the first install.

## Why the control plane is not pinned to a node

It holds a tailnet device, for outbound rather than inbound. Userspace
networking would serve workers reaching it by tailnet name; what it cannot do is
dial, and this process dials every agent's route proxy by name. So it needs
`NET_ADMIN`, `NET_RAW` and a real `/dev/net/tun`.

None of those is a reason to choose hardware. The capabilities are a property of
the pod and the device node is a property of the node image every Auto Mode node
already runs, so nothing here names a node group, a taint or an instance type.
Exposing the control plane through the Tailscale operator instead would answer
the inbound half and leave the outbound half needing `tailscaled` anyway.

## Requests, and the node count that follows from them

Karpenter provisions from what the pods request, which makes a request an
instruction to the cluster rather than a description of a process. Every
container in this chart states one, including the Jobs and the init containers,
because a pod that requests nothing is not one the node's arithmetic can see.
It is provisioned around, and then run anyway.

The figures come from `/api/v1/nodes/<node>/proxy/metrics/resource` on a live
node, not from estimates. Memory carries a limit; CPU does not, because a CPU
limit is throttling, and throttling a connector or an API turns contention into
the latency the request was meant to prevent.

`control-plane` and `cloudflared` also spread one replica per node. That is what
makes their second replica redundancy rather than cost, and it is what obliges
the cluster to hold more than one node: `DoNotSchedule` leaves the second
replica Pending, and Pending is the only thing Karpenter provisions for. A
`PodDisruptionBudget` on each is the other half. Spreading decides placement,
and only a budget stops one drain removing both.

## Replica counts

`scheduler` at one is a capacity decision: it serialises on Redis token locks and
tolerates overlapping ticks, so more is safe once there is load to justify it.

`cache-server` at one is a correctness decision: it serves a local directory, so
a second replica is a second cache rather than a larger one.

`cloudflared` runs several deliberately. Cloudflare balances a tunnel across its
connectors, and one was a single point of failure that also collided with any
other process holding the same credentials.
