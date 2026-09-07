# Deployment configuration

Terraform owns resource identities, networks, IAM, secret documents and the
database server ceiling. It publishes a non-secret infrastructure descriptor to
the deployment bucket. Only Terraform writes this document. The deploy role can
read that object and push images, but cannot read Terraform state or write S3.

Helm owns application defaults, environment policy, fleet ceilings and secret
property bindings. Edit `chart/values.yaml` or `chart/environments/prod.yaml` and
deploy. No infrastructure apply is needed for those changes. Runtime processes
receive environment variables and mounted files, never Terraform output files.

The chart explicitly selects native S3 with an empty object-store endpoint and
virtual-hosted bucket addressing. The S3 client uses the AWS credential chain
unless credentials are supplied, so production uses Pod Identity without static
keys. Development credentials belong in Compose, not client defaults.

Postgres owns customer configuration and workload state. Redis owns coordination.
Immutable manifests describe release artifacts. The deployment branch records
three explicit URLs alongside configuration and control-plane images:

- `LAZYCLOUD_RELEASE_MANIFEST_URL` supplies the control-plane release and customer authorization template.
- `LAZYCLOUD_RELEASE_WORKER_MANIFEST_URL` selects the worker image.
- `LAZYCLOUD_RELEASE_HOST_MANIFEST_URL` selects the host agent executable and AMIs.

Routine Ship advances control and worker pins, retaining the recorded host pin.
Pass `host_manifest_url` only for an intentional host upgrade. The first deployment
must supply it. The CLI and application do not pick a newer release
from S3. Customer-connected accounts remain database-owned; Helm governs only
the platform fleet. Fleet ensure refuses a changed account, role, external ID or
network instead of replacing or adopting the existing connection.

## Deploy sequence

Deploy captures the current branch revision and the infrastructure descriptor,
validates the descriptor and environment values, and renders the exact Helm
chart. It verifies the separately selected host artifact and worker image before
building control-plane images. Only after all images exist does it publish the
configuration and all three pins together, using a compare-and-swap branch push.
The control-plane release must contain the same managed-package sources as the
control plane. Changes to those packages require Ship to publish matching worker
artifacts; a code-only deploy may retain the release when those sources match.
Preflight also checks database backend capacity during rollout, including direct
connections from a deployment that predates PgBouncer. Failure before the push
leaves the selected deployment unchanged. A partially
published image set fails explicitly because commit tags are immutable.

CI authenticates to ECR Public before inspecting the worker image. Its deploy
role receives token-issuance permissions, not public-registry publishing rights.
AWS documents the required permissions in
[ECR Public authentication](https://docs.aws.amazon.com/AmazonECR/latest/public/public-registry-auth.html).
Authentication failures stop preflight; they do not mean an image is missing.

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
   current infrastructure descriptor. Never accept a database replacement or a state rewrite
   to get past a provider read failure.
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
operator-managed document. Terraform continues to own the platform document;
the WireGuard bootstrap owns its key document.

For rotation, refresh External Secrets first. Poll its Ready condition and refresh
time and the destination Secret's resource version without printing its contents.
Then bump the affected `secretRevisions` values and deploy. Existing environment
variables and subPath mounts do not refresh themselves. Test the authenticated
operation after rollout. Keep the predecessor credential valid during overlap
where the provider supports it. Database credentials affect API, scheduler,
gateway and bootstrap jobs; cache-token changes affect cache, API and scheduler.

Changing WireGuard server keys requires a separate peer migration. Do not rotate
them by editing the document or bumping a Helm revision.

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
