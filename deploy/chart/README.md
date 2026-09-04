# LazyCloud chart

The control plane, the scheduler, the cache, the tunnel, and the bootstrap that
has to run before any of them.

Values come from the deployment module rather than being authored here:
`image` from what the deploy pushed, `runtime` from `runtime_configuration`,
`secrets.map` from `secret_environment`, and `secrets.readerRoleArn` from
`secrets_reader_role_arn`. Deploy renders them into
`values-deployment.yaml` on the deployment's branch, and the deployment's Argo
Application reads that file beside `values.yaml`. Nothing in this chart decides
a value the infrastructure already knows.

One chart, one namespace per deployment. The chart never names a namespace;
Argo's Application does. It declares nothing cluster-scoped: the storage class
its claims name belongs to `deploy/platform-core`, because two deployments
syncing a chart that declared it would each claim it.

`billing.ratesEffectiveAt` is the exception, and it is authored here on purpose.
No infrastructure output knows the instant a rate card starts applying, and
nothing may derive one, because a rate boundary is a figure customers are charged
either side of. It changes in the same commit as the figures in
`packages/shared/src/shared/billing_rate_card.py`.

The reviewed cutover is September 11, 2026 at 00:00 UTC. Egress becomes
$0.13/GiB then. Compute and storage prices stay unchanged. The public catalog
states that effective date. `billing publish-rates` requires the same boundary
and publishes the reviewed history in one transaction. A fresh installation
gets the previous rates through the cutover, so starting before September 11
does not create unpriced usage. Existing rate rows are verified and preserved;
completed ledger segments are never changed.

Before deployment, preview with `lazycloud-admin billing publish-rates
--effective-at 2026-09-11T00:00:00Z`. The bootstrap Job adds `--confirm` during
sync. If deployment is delayed beyond the cutover and usage has already been
priced after it, publication refuses. Choose a new future date in the reviewed
rate card and this value before deploying. Do not change an existing rate
boundary or ledger row to make the sync pass.

## The order the bootstrap runs in

Sync waves, not preference. The schema must exist before an administrator can be
created against it, and the administrator must exist before anything
authenticates. The rate card is published into that schema last, so the
deployment can price usage from the moment it serves any.

Each Job that opens a database is alone in its wave. The chart refuses to render
when the pools it declares can exceed the server's connection ceiling, and the
sum it checks counts one Job's pool rather than every Job's.

The administrator credential is the one with a trap in it. `auth bootstrap`
adopts a configured credential when it finds one and mints its own when it does
not, recording a different bootstrap request id for each. Install without
`administrator-token` written to Secrets Manager and the credential exists only
inside that Job's pod, every later step has no bearer token, and supplying the
value afterwards is refused as an already completed bootstrap. The way back is
resetting the schema.

Write it before the first install.

## Private network

Each control-plane pod has a `wireguard-platform` sidecar in the same network
namespace. The StatefulSet ordinal selects a stable platform keypair, so
`controlPlane.replicas` and `wireguard.platformPeers` must match. The sidecar
needs `NET_ADMIN` and `/dev/net/tun`; those are pod requirements, not reasons to
select an instance type.

The separate `tunnel-gateway` Deployment exposes UDP 51820 through a
`LoadBalancer` Service. `runtime.LAZYCLOUD_WIREGUARD_PUBLIC_ENDPOINT` is the
stable host and port agents receive at enrollment. The host may use any DNS
provider as long as it reaches that UDP service.

The `wireguard-bootstrap` Job initializes one Secrets Manager document with the
gateway keypair and the small set of platform keypairs. External Secrets mounts
them as read-only files. Agent private keys remain on their machines; Postgres
stores public peer records, and Redis stores only the active-gateway lease.

## Secrets

The `SecretStore` presents the `secrets-reader` service account rather than the
operator's own identity. The External Secrets operator is installed once for
every namespace, so it holds no AWS identity; it mints a token for that account
and exchanges it for the role the account's annotation names, and the role's
trust admits this namespace's subject only. That is the one role ARN in the
chart. Every other identity is a Pod Identity association the deployment
module declares beside the cluster.

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

`control-plane`, `scheduler`, `cloudflared`, and `tunnel-gateway` spread
replicas across nodes. That is what turns their second replicas into redundancy, and it obliges
the cluster to hold more than one node. `DoNotSchedule` leaves the second replica
Pending, and Pending is the state Karpenter provisions for. A
`PodDisruptionBudget` on each prevents one drain from removing both replicas.

## Replica counts

`scheduler` at two is an availability decision, not capacity: it serialises on
Redis token locks and tolerates overlapping ticks, so the second replica adds
nothing to throughput and keeps placement running while a node is replaced.

`cache-server` at one is a correctness decision: it serves a local directory, so
a second replica is a second cache rather than a larger one.

`tunnel-gateway` runs two replicas against one gateway key. Redis grants the
active lease to one replica and the other remains ready to take over. This is
availability, not packet-capacity scaling; both replicas do not forward traffic
at the same time.

`cloudflared` runs several deliberately. Cloudflare balances a tunnel across its
connectors, and one was a single point of failure that also collided with any
other process holding the same credentials.
