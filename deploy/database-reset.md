# Relational baseline reset

This runbook applies only to the owner's approved reset of production and local
Compose for PR #295. It destroys application data and migration history. Future
releases use forward Alembic migrations from `0001_relational_baseline`.

Status: local Compose reset, bootstrap and live acceptance completed. Production
has not been changed.

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

Production's recorded subscription was independently read. It is a live free
subscription with one paid zero-dollar invoice and no pending invoice items.
It has not been canceled or changed.
