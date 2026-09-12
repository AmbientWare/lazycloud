# Deployment configuration

Terraform owns resource identities, networks, IAM, secret documents and the
database server ceiling. It exports a non-secret infrastructure descriptor.
The operator publishes that output to the private S3 deployment bucket with
`python -m deploy.object_storage publish`. Deploy downloads its exact descriptor
with its OIDC role, which has no Terraform-state access.

Python provider definitions own purchase enablement, approved locations,
instance catalogs and supplier price assumptions. `compute.fleet_policy` owns
fleet limits and warm targets. Helm owns Kubernetes resources, process settings
and secret property bindings. Edit their owner and deploy. Runtime processes
receive environment variables and mounted files, never Terraform output files.

The values renderer supplies provider image identities from the infrastructure
descriptor and binds the credentials needed to manage them. Disable new
purchases through the provider definition's `policy.purchases_enabled` field.
Retain existing infrastructure identities and credentials until node cleanup
finishes.

The chart supplies one S3-compatible endpoint, signing configuration,
application bucket, workspace bucket prefix and workspace grant role.
Archives share the application bucket. Workspace mounts receive temporary,
bucket-scoped STS credentials. Platform pods use AWS Pod Identity. Customer-owned
storage uses customer credentials through the same storage contracts.

Postgres owns customer configuration and workload state. Redis owns coordination.
Immutable manifests describe release artifacts. The deployment branch records
one `LAZYCLOUD_RELEASE_MANIFEST_URL` alongside configuration and image digests.
The manifest selects platform images, the worker image, agent executable,
authorization template, and host AMIs together. Ship supplies that URL automatically.
Customer-connected accounts remain database-owned.
Fleet ensure refuses a changed account, role, external ID or
network instead of replacing or adopting the existing connection.

## Deploy sequence

Deploy captures the current branch revision and the infrastructure descriptor,
validates the descriptor and environment values, and renders the exact Helm
chart from the complete manifest Ship already published. Deploy builds nothing.
It checks database connection budgets and commits the configuration with a
compare-and-swap branch push. A failed build or partial publication cannot select
a deployment because the complete manifest has not been published.

Argo rolls out all chart resources and applies the active-release ConfigMap in
PostSync after health checks pass. API and scheduler read its mounted file on work
admission. A replica whose configured manifest differs refuses new work; maintenance,
drain, logs and completion traffic continue. Workers must report the selected worker
image and agent binary before accepting new work. Existing registration and drain
state provide rollout status. No release tables or service observer are required.

Rollback selects an earlier complete manifest with a newer activation generation.
Managed hosts follow the launch-template replacement controller. Supervised joined
agents update after their workloads drain. Long-running pods can hold that drain.

Cloudflared configuration and the AWS role-chain ConfigMaps have pod-template
checksums. A change to a mounted configuration therefore rolls its consumers.

## PostgreSQL connections

Terraform publishes two secret properties. `LAZYCLOUD_DATABASE_URL` names
PlanetScale's built-in transaction pooler on port 6432 for application SQL.
`LAZYCLOUD_DATABASE_DIRECT_URL` names port 5432 for migrations, administrator
commands, and session advisory locks. Neither endpoint is inferred from the other.
Missing direct configuration fails the operation that requires it.

Each API replica has one bounded direct connection for workspace deletion and
one for its recovery fence. Direct connections use autocommit and close their
physical backend on exit, even if unlocking fails. Application transactions set
their statement timeout with `SET LOCAL`, not startup options. Protocol prepared
statements remain enabled; clients require libpq 17 or newer. PlanetScale's
default `max_prepared_statements` is 200. See the
[PlanetScale connection guidance](https://planetscale.com/docs/postgres/connecting/pgbouncer)
and [psycopg requirements](https://www.psycopg.org/psycopg3/docs/advanced/prepare.html).

Terraform sets `max_db_connections` to bound the local pooler at 20 backend
connections per database, across all user pools. Other pooler settings retain
PlanetScale's defaults; its API omits overrides equal to those defaults.
The infrastructure descriptor exports that bound to Helm. With two API
replicas, two bootstrap connections and three reserved connections, the normal
backend budget is 29 against a server ceiling of 40. Application client pools
are separate from this backend budget.

For the first migration from direct application connections, start from a stable
deployment with no rollout in progress:

1. Review and apply Terraform with `database_pooler_max_connections=3`, retaining
   the existing database, role and password. It publishes both URLs and the
   current infrastructure descriptor. Never accept a database replacement or a
   state rewrite to get past a provider read failure.
2. Run Ship. Existing pods keep their direct URL until replaced; the old 28
   application connections plus three pooler backends, four direct lock
   connections, two bootstrap connections and three reserved connections total 40.
3. Verify every application pod runs the new release and port-6432 configuration.
   Check database sessions, successful workload enrollment, task completion and
   worker logs. Only lock holders and administrator operations should remain direct.
4. Apply Terraform with the normal bound of 20. Record its descriptor through
   the normal deploy workflow, preserving the same application and release pins.

Future deployments use the normal bound. Changing a client pool does not change
the pooler's server allocation. Keep the operator reserve and direct lock budget
when changing the server ceiling or replica count.

## Credential changes

Add a property binding under `secrets.map` and include it only in the required
consumers under `environment`. Preserve unrelated properties when updating the
operator-managed document. It also holds the tunnel issuer and dedicated gateway
bootstrap credential. Terraform continues to own the platform document.

For rotation, refresh External Secrets first. Poll its Ready condition and refresh
time and the destination Secret's resource version without printing its contents.
Then bump the affected `secretRevisions` values and deploy. Existing environment
variables and subPath mounts do not refresh themselves. Test the authenticated
operation after rollout. Keep the predecessor credential valid during overlap
where the provider supports it. Database credentials affect API, scheduler,
gateway and bootstrap jobs; cache-token changes affect cache, API and scheduler.

The tunnel CA needs a planned trust migration. Bootstrap never rotates it.
See [connection gateway deployment](connection-gateway.md).

## Pause and resume

After the core ownership change has been applied, pause the child Application:

```sh
kubectl -n argocd patch application lazycloud-prod --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"enabled":false}}}}'
```

Poll the child field, root sync status, active operation and pod revisions in
short cycles. The field must remain false across root reconciliation. If a sync
is already running, terminate it through the authenticated Argo CLI or UI and
confirm its operation phase changed. An autosync pause alone does not terminate
an active operation.

Argo termination does not undo already-applied Kubernetes objects. Deployment
and StatefulSet controllers can continue replacing pods. Inspect their revisions
and replica counts before deciding whether to freeze a controller or roll back.
Do not report a deployment stopped while its Kubernetes rollout is still active.

Resume only after reviewing the recorded branch and current workload health:

```sh
kubectl -n argocd patch application lazycloud-prod --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"enabled":true}}}}'
```

Resume can immediately apply the branch's recorded release. Pausing does not
cancel queued GitHub workflows, so stop those separately if no new deployment
record should be published.
