# LazyCloud chart

Use this chart through the deployment workflow. It runs the APIs, scheduler,
cache, connection gateways, HTTP ingress, and bootstrap Jobs. Argo owns the
installed resources; use local Helm rendering to review changes before deployment.

`control-plane` serves account, workspace, deployment and dashboard operations.
`execution-api` serves invocations, tasks, workload HTTP, pod and shell streams,
and public TCP ingress. `runtime-api` receives worker control calls, task claims,
completion and metering. Cloudflare selects the destination directly from the
public hostname and path. Each connection gateway has an HAProxy sidecar that
routes tunneled worker and container SDK requests to the same owners.

Runtime replicas own agent route, disconnect and provider reconciliation.
Management and execution replicas do not start these recurring scans.

API pods keep serving for 7 seconds after termination starts so Service routing
can remove their endpoints before Uvicorn closes its listeners. Uvicorn then
allows existing requests 120 seconds to finish. The 150-second pod grace period
includes that delay and leaves 23 seconds for service cleanup.

The gateway router binds only to loopback and starts before the gateway. Kubernetes
keeps this sidecar running while the gateway drains. Each router requests 50m CPU
and 32Mi memory, with a 256Mi memory limit and no CPU limit.

Execution constructs workload admission, task reads, storage and transport owners.
Its app and deployment dependencies only read durable state. Runtime adds worker,
provider and disk control. Management adds identity provisioning, payment setup,
app and deployment mutations, custom domains and deletion. Execution does not
download agent artifacts or hold fleet provisioning configuration.

Image archives share the application S3 bucket and workload identity.
Infrastructure descriptor version 8 supplies its endpoint and bucket identities,
the workspace grant role, and the platform provider's stable reference, node IAM
identity, and regional networks. Helm passes the AWS binding directly to platform
initialization, the API, scheduler, and ownership migration.
See [object storage](../platform-deployment/OBJECT_STORAGE.md) for identity,
verification, and data-preservation requirements.

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

## Bootstrap order

Sync waves order secret projection, migrations, administrator and billing
bootstrap, and workloads. Inspect the first failed Job before retrying a sync.

Each database Job is alone in its wave. Transaction clients share the bounded
PgBouncer backend pool. Every API process also holds one direct recovery-fence
session, which prevents offline administrator recovery while an API serves.
Management also permits one direct workspace-deletion lock per replica. The chart
includes both lock owners, one bootstrap Job and rollout overlap in the server
connection calculation. Scheduler readiness covers every loop.
Each gateway becomes ready after certificate issuance, listener startup, and
successful database and Redis probes.

Database migrations run while previous replicas serve requests. Handoff sources,
purchase demand IDs and fulfillment timestamps live in dedicated columns, which
older JSON writers preserve. Check reads and writes from both application
versions before rollout.

The database rejects writes that reopen a terminal capacity operation or replace
an owned container assignment. Older replicas may report these errors during
rollout. A rejected write leaves the recorded ownership intact.

Populate the administrator token in the operator secret before the first sync.
A bootstrap without that configured value can leave later Jobs without the
credential they need. Recover a lost credential through the account owner;
never reset the persistent database.

## Log retention

Task log history lives in PostgreSQL. Free accounts retain 1 day, Team accounts
retain 30 days, and Business and complimentary accounts retain 90 days, according
to the workspace owner's entitlements.
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

## Public TCP ingress

Public TCP workloads share a Network Load Balancer on port 1995. It passes TLS
through to the execution API, where the SNI hostname selects the workload and
port. Clients must support TLS and send SNI. The load balancer preserves client
addresses for transfer attribution.

Terraform in `deploy/cloudflare` owns the DNS-only `*.tcp.<apex>` CNAME. Creating
or deleting a workload changes no DNS records, certificates, or load balancers.
After the Service is provisioned, set Terraform's `tcp_ingress_endpoint` to its
exact NLB hostname and review the plan before applying it.

The shared `cert-manager` Argo application installs the certificate controller.
The namespace's `tcp-ingress` Issuer obtains a Let's Encrypt wildcard certificate
using Cloudflare DNS validation and renews it automatically. Only this controller
uses `tcp-ingress-dns`. The execution API mounts `tcp-ingress-tls` and reloads the
certificate without restarting.

Before releasing this chart, save the existing deploy Cloudflare token as
`LAZYCLOUD_TCP_DNS_API_TOKEN` in the deployment's operator secret document,
preserving all other fields. This reuses the same credential; it does not create
another Cloudflare token. It needs Zone Read and DNS Edit for the installation's
zone. External Secrets reads this property into the certificate controller's
Secret; it is not an application environment variable. Renewal uses the token's
existing permissions.

Merge the cert-manager Application and verify its controller, webhook, and CRDs
before deploying the chart. Verify the DNS ExternalSecret, Issuer, and Certificate
conditions during rollout. A missing token or certificate prevents new API pods
from starting; existing replicas keep serving during the rolling update.

Acceptance uses public Pods with `tcp=True`, `authorized=False`, and named ports.
Connect to their returned `tls://` URLs with standard certificate verification,
check each workload's response, and verify that deleting a deployment rejects
new connections. Check certificate renewal and reload, then replace a serving
replica and verify new connections through the remaining replica. Remove only
the acceptance workloads. Monitor certificate expiry and renewal failures; if
the NLB is replaced, update its Terraform input and apply the reviewed DNS change.

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

## Size and place pods

Auto Mode provisions from declared pod requests. Measure representative usage
before changing them, and include bootstrap Jobs and init containers in the
budget. Keep memory limits and preserve headroom for startup and reconnects.

The APIs, scheduler, cloudflared and connection gateway spread
replicas across nodes and availability zones. Both constraints require two
domains and count old and new revisions together. A missing domain leaves a
replica Pending so Auto Mode provisions capacity. Disruption budgets permit one
replica to drain at a time; they cannot prevent a Spot interruption.

Each API starts with two replicas requesting 640Mi of memory and a 1Gi memory
limit. CPU requests are 150m for management, 300m for execution and 250m for
runtime, without CPU limits. The previous combined API measured 529Mi resident
memory before this split. Recheck resident memory, event-loop lag, executor
queue time and database checkout time under mixed workload traffic before
reducing requests or increasing replica counts.

Management permits four transaction connections across its synchronous and
asynchronous pools, including overflow. Execution and runtime each permit six.
At two replicas per API, the pools permit 32 transaction client connections.
The backend pool remains bounded independently. Direct recovery and management
deletion sessions are budgeted separately, including terminating replicas. The
default backend budget is 37 against the deployed ceiling of 40.

API rollouts replace one replica at a time without an active surge. A replacement
must remain ready beyond the previous pod's termination grace before the next
replacement. Execution and runtime install in wave 0, ingress and connection
gateways switch in wave 1, then management installs in wave 2. This prevents
management from removing a route before its new owner is ready. Terminating pods
can retain memory and direct sessions until their grace expires.

The initial execution-scaling cutover requires admission to stop and existing
claims, queued requests and image builds to drain before the new runtime and
scheduler accept work. The new claim and fairness contracts cannot be mixed
with older writers. Ordinary rolling updates resume after that cutover.

## Replica counts

Two scheduler replicas preserve availability during node replacement. Redis
locks coordinate their work; raising replicas alone does not raise throughput.

`cache-server` has one replica and one ReadWriteOnce EBS volume. A replacement
must run in the volume's zone and attach the same disk. Cache service is
unavailable during that recovery. Do not increase replicas against this claim.
Its readiness probe checks the listener; acceptance must also read stored data.

Connection gateways share one TCP Service and NLB. Each pod holds its own
short-lived certificate and session identity. Readiness checks its listener,
certificate, database, and Redis access. See the gateway guide for drain limits.

Cloudflare connectors report readiness after connecting to the edge. Keep
replicas on separate nodes and verify public requests after a rollout.

Execution scheduling and fleet control run as separate deployments using the
scheduler image. The scheduler requests 400m CPU and 512Mi memory per replica;
the fleet controller requests 250m CPU and 512Mi. Each has two replicas and a
1Gi memory limit. CPU is not limited. Scheduler database pools allow five
connections per replica; fleet pools allow three. Both use the bounded pooler.

Execution scheduling receives no payment, email, DNS or fleet credentials.
The fleet controller owns machine acquisition, reserves, rollout, consolidation
and maintenance.
Execution scheduling owns placement, dispatch, cron and build submission.
The scheduler rolls in wave 3 and fleet control in wave 4, after the API cutover,
with no surge replicas. Health checks read each process's own loop heartbeats.
