# LazyCloud chart

The control plane, the scheduler, the cache, the tunnel, and the bootstrap that
has to run before any of them.

Image archives share the application S3 bucket and workload identity.
Infrastructure descriptor version 7 supplies its endpoint and bucket identities,
the role that issues temporary workspace credentials, and `fleet.networks` keyed
by AWS region. Fleet registration passes that map to `fleet ensure --networks-json`.
Follow the
[S3 cutover steps](../platform-deployment/OBJECT_STORAGE.md) before deploying
these settings to an existing installation.

Helm owns application defaults in `values.yaml` and environment policy in
`environments/<environment>.yaml`. Terraform owns resource identities and publishes
`configuration/infrastructure-v1.json` to the deployment bucket. It contains no
credentials. Deploy snapshots that document with the environment values into
`values-deployment.yaml`, then Helm merges it over this chart's defaults.

The snapshot records images and the selected immutable release in the same Git
commit. CI validates it before building or pushing the deployment branch. A
code-only deploy needs no Terraform apply. Adding an application setting or a
secret property binding needs no Terraform apply either.

One chart, one namespace per deployment. The chart never names a namespace;
Argo's Application does. It declares nothing cluster-scoped: the storage class
its claims name belongs to `deploy/platform-core`, because two deployments
syncing a chart that declared it would each claim it.

The billing bootstrap publishes the reviewed history from
`packages/shared/src/shared/billing_rate_card.py`. That history owns both prices
and effective dates; Helm has no rate-date setting.

Before deployment, preview with `lazycloud-admin billing publish-rates`.
The bootstrap Job adds `--confirm`
during sync and publishes the reviewed history in one transaction. Existing
rates and completed ledger segments are preserved. If publication conflicts
with usage already priced, review a new future card before deploying. Do not
change an existing rate boundary or ledger row to make the sync pass.

Rate card `2026-09-10.a` was activated at `2026-09-10T04:09:05.835918Z`.
Migration `0030_withdraw_rate_schedule` removes only the unused September 11
and 12 schedules, refusing withdrawal if either has priced ledger entries.
The recorded activation time makes subsequent publication idempotent and
preserves all charges before the cutover.

## The order the bootstrap runs in

Sync waves, not preference. The schema must exist before an administrator can be
created against it, and the administrator must exist before anything
authenticates. The rate card is published into that schema last, so the
deployment can price usage from the moment it serves any.

Each database Job is alone in its wave. The chart budgets both API engines,
scheduler and gateway pools, one bootstrap Job, and bounded workload rollout
overlap against Terraform's server ceiling. Scheduler readiness covers every loop.
Each gateway becomes ready after certificate issuance, listener startup, and
successful database and Redis probes.

Database migrations run while previous replicas serve requests. Handoff sources,
purchase demand IDs and fulfillment timestamps live in dedicated columns, which
older JSON writers preserve. Check reads and writes from both application
versions before rollout.

The database rejects writes that reopen a terminal capacity operation or replace
an owned container assignment. Older replicas may report these errors during
rollout. A rejected write leaves the recorded ownership intact.

The administrator credential is the one with a trap in it. `auth bootstrap`
adopts a configured credential when it finds one and mints its own when it does
not, recording a different bootstrap request id for each. Install without
`administrator-token` written to Secrets Manager and the credential exists only
inside that Job's pod, every later step has no bearer token, and supplying the
value afterwards is refused as an already completed bootstrap. Do not reset a persistent installation to recover a credential. Resolve the
bootstrap identity through the account owner.

Write it before the first install.

## Log retention

Task log history lives in PostgreSQL. Free accounts retain 1 day; Team and
complimentary accounts retain 30 days, according to the workspace owner's plan.
Reads enforce the cutoff immediately. Downgrading applies the shorter window;
deleted logs cannot be recovered by upgrading later.

The `log-retention` CronJob runs every 15 minutes, outside the scheduler. It runs
`lazycloud-admin --json maintenance prune-logs` with one database connection and a
5-second statement timeout. Each transaction deletes at most 1,000 rows; a run
stops after 100 batches or 4 minutes. Kubernetes prevents overlapping scheduled
runs and terminates a Job after 5 minutes. The command reports `deleted`,
`batches`, and `budget_exhausted`; repeated exhausted runs mean cleanup is
falling behind. Failures exit nonzero and remain visible in Job logs.

Local Compose runs the same command every 15 minutes in the `log-retention`
service. Inspect it with `docker compose logs log-retention`, or run one pass
with `docker compose run --rm --no-deps --entrypoint lazycloud-admin log-retention
--json maintenance prune-logs`.

## Agent connections

The API and `connection-gateway` are independent Deployments. Two gateway replicas
share a TCP443 NLB that forwards encrypted traffic to port8443. Each process has
its own identity and short-lived certificate. The API alone mounts the issuer key.
Agents connect outbound; API pods need no sidecar, network privileges, or stable
ordinal. PostgreSQL owns enrollment and authorization, while Redis holds expiring
connection ownership. See [connection gateway deployment](../connection-gateway.md)
for initial credentials, DNS, rollout, drain limits, and acceptance.

## Secrets

The chart maps named properties to the platform or operator secret document.
Each container has an explicit list in `environment`; artifact downloads receive
no secrets, and the cache receives only its own token. The cache also has no AWS
Pod Identity association.

A refreshed Kubernetes Secret does not update environment variables or subPath
mounts. Refresh the ExternalSecret first, verify its status without printing secret
values, then bump the affected `secretRevisions` entries. Jobs receive current
values on their next run. See [configuration transition](../CONFIGURATION.md).


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

`control-plane`, `scheduler`, `cloudflared`, and `connection-gateway` spread
replicas across nodes and availability zones. Both constraints require two
domains and count old and new revisions together. A missing domain leaves a
replica Pending so Auto Mode provisions capacity. Disruption budgets permit one
replica to drain at a time; they cannot prevent a Spot interruption.

## Replica counts

`scheduler` at two is an availability decision, not capacity: it serialises on
Redis token locks and tolerates overlapping ticks, so the second replica adds
nothing to throughput and keeps placement running while a node is replaced.

`cache-server` has one replica and one ReadWriteOnce EBS volume. A replacement
must run in the volume's zone and attach the same disk. Cache service is
unavailable during that recovery. Do not increase replicas against this claim.
Its readiness probe checks the listener; acceptance must also read stored data.

Each gateway index runs one replica with its own key, UDP Service and Redis lease.
Both identities forward traffic. Clients move new connections to another healthy
gateway when one drains or becomes unavailable.

Cloudflare readiness requires a connection to its edge before a replacement
counts as available. Gateway readiness requires identity ownership and a platform
handshake through its public endpoint.

`cloudflared` runs several deliberately. Cloudflare balances a tunnel across its
connectors, and one was a single point of failure that also collided with any
other process holding the same credentials.
