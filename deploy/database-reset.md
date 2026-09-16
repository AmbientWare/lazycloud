# Relational baseline reset

This runbook applies only to the owner's approved reset of production and local
Compose for PR #295. It destroys application data and migration history. Future
releases use forward Alembic migrations from `0001_relational_baseline`.

Status: both resets and live acceptance completed. Production runs release
`0.1.0`; normal service, Argo reconciliation and retention are restored.

## Targets observed before reset

| Installation | Identity | Observed schema |
| --- | --- | --- |
| Production | AWS account `534742592531`, EKS cluster `lazycloud` in `us-east-1`, namespace and Argo application `lazycloud-prod`. Terraform names the PlanetScale database `lazycloud-prod`, branch `main`; the mounted direct PostgreSQL connection uses `us-east-3.pg.psdb.cloud:5432/postgres`. | `0049_database_lookup_indexes` |
| Local | Compose project `lazycloud`, container `lazycloud-postgres-1`, database `lazycloud` at `postgres:5432`. | `0048_capacity_progress` |

Production uses AWS profile `default`. Read the namespace's existing secret through
the deployed process; do not copy production operator credentials into local
services. Confirm the current database identity and enumerate its application
objects again immediately before deletion. Never select a database by hostname
alone, since the managed endpoint is shared across branches.

The local running stack was started from the `sdk-failure-batch` worktree with
`/home/cmclean/.local/state/lazycloud-sdk-checks/serve-typed-local/compose.env` and
its override. Reuse its local credential material, but build the accepted source
from this checkout with the canonical root Compose definition and a distinct image
tag. The current root `.env` lacks required tunnel bootstrap material. Do not
regenerate the running stack's credentials to bypass that mismatch.

Preserve the unrelated `tcvknn-api-1` container and buildx builder. The temporary
`lazycloud-tests` PostgreSQL/Redis services are acceptance resources and must be
stopped when acceptance finishes.

## Resource disposition

The following observations are preliminary and must be refreshed before acting.

- Production has no pending or running containers and no unfinished tasks in the
  initial inventory. Local has no pending or running containers either.
- Production compute unit `908c3fbd-66d7-5a3b-bcdb-628ce1a68673` belongs to workspace
  `6c1181d8-d13d-418c-8faa-254a95114918`. It owns AWS ASG
  `cloud-pool-asg-managed-908c3fbd66d75a3bbcdb628c-5c53c86a45`, running instance
  `i-01de7d2a0a2e32f0e` and volume `vol-0933cb0706ca0837b` in `us-east-1`.
  Retire it through the current compute owner, then verify the provider's absence
  independently. Two older terminating records name `i-0089840af941dee2b` and
  `i-0cda7262a1fa2f04e`; the provider no longer returned them in the initial read.
- Preserve the deployment's AWS connection authority, VPCs, roles, EKS cluster,
  release buckets, image registries and Secrets Manager documents. Re-register
  deployment-owned fleet authority through the normal fleet bootstrap after reset.
- Inventory every workspace bucket and application artifact prefix before deleting
  database records. Retire deployment-owned workspace storage through its owner;
  preserve customer-owned external buckets. One volume cleanup record remains in
  production and must be resolved before its storage identity is erased.
- Production has a recorded Stripe customer and subscription. Resolve their actual
  provider state and any pending metering, purchases or plan changes before removing
  account IDs. A database reset does not cancel an external subscription or settle
  an invoice. Preserve unrelated Stripe objects and the deployment catalog.
- Production has no custom domains in the initial inventory. Recheck before reset.
- Production's administrator GitHub ID is configured in the bootstrap secret.
  It is intentionally absent from the ordinary API pod environment. Local also
  has its administrator GitHub ID configured, and its Stripe credentials are test
  credentials.

## Execution order

1. Pass the integrated tests, types, formatting and image builds. Run the new
   migration and repeated bootstrap on a disposable PostgreSQL database.
2. Finish the disposition inventory and cleanup through the old release while
   it can still interpret the old records. Prove external cleanup is complete.
3. For each installation, announce the irreversible step. Close admission, stop
   old writers, suspend the log-retention job, and disable automatic Argo sync
   during production maintenance. Preserve the prior controller settings.
4. Enumerate and remove only the selected installation's application schema.
   Clear only its Redis database or verified key namespace after stopping its
   writers. Retire old local worker sessions and reenroll them through bootstrap.
5. Bootstrap the accepted baseline, administrator identity, billing rates and
   deployment fleet authority using the existing canonical jobs. Do not stamp
   old data as current or mix old and new images against the rewritten schema.
6. Verify local deploy/invoke, retries/dependencies/cancellation, pod lifecycle,
   logs and storage with exact named scenarios under `tests/e2e/local`. Verify
   administrator linking and ordinary-user restrictions through existing owner
   acceptance and the configured sign-in path.
7. Select the accepted production release through Ship/Deploy and Argo. Keep
   admission closed until bootstrap and the new replicas work. Restore the
   controller settings and retention schedule, then verify a named production
   workload and its cleanup. Poll application state, logs and provider state
   together; a healthy pod alone is insufficient.
8. Record the deployed revision, resource cleanup and acceptance evidence here.
   Remove temporary acceptance resources. Freeze the baseline permanently.

There is no data-preserving rollback for this reset. If bootstrap or acceptance
fails after deletion, keep admission closed and repair the accepted release or
rebuild the empty installation.

## Local execution

On 2026-09-16 UTC, all PR checks passed for `351a86226`. The local Stripe test
subscription `sub_1UF6y6LpRGtVfZdqvKr2p3PT` had a zero-dollar price, one paid
zero-dollar invoice and no pending invoice items. It was canceled; its customer
and invoice history remain at Stripe.

The stopped local installation owned exactly three Garage buckets, its application
bucket and the two active workspace buckets. All persistent volumes selected below
had the `lazycloud` Compose project label and no container from another project
mounted them. The reset removed these volumes:

- `lazycloud_postgres-data`
- `lazycloud_redis-data`
- `lazycloud_garage-data`
- `lazycloud_workload-registry-data`
- `lazycloud_lazycloud-files`
- `lazycloud_lazycloud-cache`
- `lazycloud_connection-gateway-0`
- `lazycloud_connection-gateway-1`

The old agent and its worker were stopped. The worker container and its anonymous
volume were removed, and the inventoried agent directory
`/tmp/lazycloud-local-main-cvuhfh6c/agent` was emptied. Certificate authority,
ingress certificate, cache credential and administrator token volumes remain.

`python -m deploy.release` rebuilt and activated every local image from
`351a86226`, whose application code matches `3203b1592`. The image tag is
`relational-3203b1592`. The existing private Compose environment now selects this
tag and this checkout's freshly built agent artifact directory. Bootstrap reports
`0001_relational_baseline` and 79 public tables, including Alembic's revision table.
The configured administrator claim and local customer were bootstrapped, and the
agent rejoined with new authority. All services reached their health checks.

The local customer billing relationship was provisioned through
`BillingAccountService` and real Stripe test credentials, the same owner used by
sign-in. The new free subscription is `sub_1UG8ZOLpRGtVfZdqihunqQzO`.
The public Function invocation scenario built an image, uploaded source, returned
49 for `square(7)` and deleted its app. Dependencies, retries, cancellation with an
unaffected neighboring call, reruns, opaque result serialization, Sandbox exec and
termination, mounted Volume reads/writes and Volume transfers also pass. Each
scenario uses its public cleanup path.
Function log streaming and attribution pass for sequential and concurrent calls.
Repeating the database bootstrap reports the baseline already current.

Production's old subscription was independently read. It was a live free
subscription with one paid zero-dollar invoice and no pending invoice items.
It was canceled during production maintenance. Its customer and paid invoice
history remain at Stripe.

## Production execution

PR #295 merged as `ba958db9e0840d47e7661566b833a2340bab0de1` after all checks
passed. The implementation's complete CI run passed 1,893 tests. Ship run
`35048751994` publishes the merged source.

Maintenance added `argocd.argoproj.io/skip-reconcile=true` to the `lazycloud-prod`
Application, suspended the retention CronJob and scaled the five application
Deployments to zero. The prior replicas were control-plane 2, scheduler 2,
connection-gateway 2, cloudflared 2 and cache-server 1. Argo's configured automated
sync policy was preserved. Only the deployment's operator pod remained running
before the database reset.

The public compute deletion owner retired unit
`908c3fbd-66d7-5a3b-bcdb-628ce1a68673`. The first request encountered a scheduler
lease; after the scheduler stopped, deletion requested provider termination and a
subsequent request returned 204. Independent AWS reads confirmed the ASG, launch
template and attached volume absent, and no pending, running, stopping or stopped
instances tagged with the three workspace IDs in either configured AWS region.

Workspace `c4a4e80c-a617-4069-9bf6-ea058de8903a` completed its public deletion.
The old release could not finalize `mclean-connor` because historical worker
shutdown acknowledgments were missing; its default workspace cannot be deleted
through the public route. After infrastructure shutdown was independently proved,
the deployed storage issuer retired those two exact workspace buckets, fencing
issued grants and purging their objects through its production implementation.
AWS then listed no remaining deployment workspace buckets.

The application bucket held 47 image archives and 19 workspace source objects,
1,440,646,617 bytes total. It had versioning disabled and no unfinished multipart
uploads. Those inventoried objects were removed after writers stopped; the
deployment bucket itself remains, empty. Release buckets, ECR repositories,
networking, roles, authorization infrastructure and other accounts were preserved.

The database reset verified the mounted direct target, revision
`0049_database_lookup_indexes`, and exact equality between the 82 public tables
and the deployed metadata plus Alembic. It dropped those tables and the three
application trigger functions in one transaction. Schema ownership, grants and
extensions remain. No revision was stamped over old data.

The Redis reset verified the deployment's database 0 endpoint and enumerated the
keys. It removed 3,912 keys belonging to `lazycloud:` and the application's health
rate limiter, then verified zero remained. It did not flush another database.

The temporary `relational-reset-operator` pod held only the database and Redis
configuration needed for reset verification. Cluster consolidation evicted and
removed it after both resets were verified. The standalone `lazycloud-tests`
services and local acceptance credentials have also been removed.

## Completed deployment

Ship run `35048751994` succeeded. Release `0.1.0` names source
`ba958db9e0840d47e7661566b833a2340bab0de1`; the deployment branch records it at
`afc7743bd1de00e736904ba471b100a4a1cd1b84`. Argo reports Synced and Healthy.

On resuming reconciliation, the controller briefly restored the old Deployment
replica counts while refreshing its branch revision. They were immediately held
at zero again. The new migration job completed with `0001_relational_baseline`
before application rollout continued. Database, administrator, billing catalog,
billing rates and fleet bootstrap jobs all completed from the new release.

The production database reports 79 public tables including Alembic. The GitHub
identity was compared directly with the configured bootstrap ID and belongs to
the active administrator. Both API and both scheduler replicas read active release
generation 35, version `0.1.0`, with the merged source revision. All five
Deployments have their original replica counts and healthy replicas. The temporary
Argo skip annotation is removed, its original automatic sync policy remains,
and log retention is unsuspended with the new CLI image.

The administrator's new free billing relationship was provisioned through the
same `BillingAccountService` used by sign-in. Its subscription is
`sub_1UG8ynLpRGtVfZdqBrjRWxu9`. No OAuth session was fabricated; a browser sign-in
will authenticate the already-linked GitHub identity.

The public production SDK deployed `relational_reset_acceptance_20260916`,
built its Python image on the new managed fleet and invoked one Function.
Task `9d1a347e-7728-4e7a-8a90-166d305be201` completed with
`{"square": 49, "values": [7, null, true]}` and streamed its execution marker.
The app was deleted through the public API and verified absent. Its Function
container is stopped and image-build container exited. The deployment's normal
two-machine warm fleet remains; acceptance did not create a separate pool.

`0001_relational_baseline` is now frozen. Future schema changes add forward
migrations. This completed reset authorizes no subsequent production reset.
