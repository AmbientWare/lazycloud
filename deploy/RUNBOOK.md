# Deployment runbook

Start with [local setup](README.md#local-environment) for Compose or
[deployment creation](platform-deployment/LIFECYCLE.md) for a new hosted
installation. The procedures here operate an existing deployment. Run commands
from the repository root and confirm the selected environment before mutations.

## Start a local deployment

```bash
cd /path/to/lazycloud
uv run --frozen --group workspace python -m deploy.release
```

The command builds the Compose images, reports service health, and activates the
release after the platform is healthy.

### Billing, before the first sign-in

Publish the billing catalog and rate history before admitting users. Missing
catalog entries can block account provisioning. Usage recorded before rate
publication remains unpriced until an operator prices that interval.

A cluster deployment runs both from the chart on every sync, as the
`billing-catalog` and `billing-rates` Jobs in
`deploy/chart/templates/bootstrap-jobs.yaml`. The commands below are the same
work for the Compose stack, which has no Argo to run them.

```bash
uv run --group workspace lazycloud-admin billing publish-catalog --confirm-account acct_...
uv run --group workspace lazycloud-admin billing publish-catalog --confirm-account acct_... --confirm

uv run --group workspace lazycloud-admin billing publish-rates
uv run --group workspace lazycloud-admin billing publish-rates --confirm
```

Preview both commands without `--confirm`, then publish after checking the
target account and rates. They preserve published values and reject conflicts.
`LAZYCLOUD_STRIPE_WEBHOOK_SECRET` must be set before the first card is saved. The
endpoint refuses every delivery without it, and Stripe disables endpoints that
keep failing.

Usage metered before the rates were published stays unpriced. Nothing revisits
it, so charge it with a window you name:

```bash
uv run --group workspace lazycloud-admin billing price-unpriced --from <iso> --to <iso>
uv run --group workspace lazycloud-admin billing price-unpriced --from <iso> --to <iso> --confirm
```

The start cannot be older than the 35 days Stripe accepts meter events for; the
command refuses beyond that rather than freezing a cost no invoice can carry.

## Publishing a release

Build and activate local Compose images:

```bash
uv run --group workspace python -m deploy.release
```

It derives services from Compose, builds their images, restarts the stack, and
polls container state, health and image identity. Once the platform is healthy it
writes `.lazycloud-release/active.json`, mounted by API and scheduler. Worker
registration uses the built image ID from `.env.release`. Both files are local
state. The same release admission code runs in Compose and Kubernetes.

### Shipping from Actions

The `platform initialize` job creates the deployment namespace and validates its
provider identities before the API and scheduler start. Deployment and worker
credentials require no human account or token. Create administrator access
separately with offline `auth bootstrap`; use offline recovery if access is lost.
Revoked human credentials stay revoked during releases.

The `0005_platform_namespace` upgrade requires a stopped-writer cutover. Follow
[platform ownership migration](platform-deployment/PLATFORM_OWNERSHIP.md).

The Ship workflow is the release. Run it from `main` and choose `patch`,
`minor`, or `major`; it reads the latest `v*` tag, pushes the next one, publishes
the release under that version, and deploys onto it. The tag is the only record
of the version. The workspace keeps `0.1.0` as a placeholder and the release
stamps the real number at build time, so nothing in the tree is bumped.

The same run publishes `lazycloud-client` and `lazycloud-shared` to PyPI at
that version, so a customer's installed version names the release it came from.

A release rebuilds only the application artifact whose inputs changed.
`deploy/aws-release-assets/plan.py` compares the commit with the previous release
and decides whether to rebuild the worker image and agent binary. Host images
come from the separately dispatched Node Images workflow. Release resolves its
current catalog and refuses to continue if the catalog's recipe does not match
the checked-out revision. Worker changes never start an AMI bake. The complete
manifest includes that catalog and retains unchanged worker and agent artifact
identities. Ship builds platform targets from Docker Bake, resumes existing
commit images after a partial push, then publishes the complete manifest.

Argo applies the chart and activates the release after its health checks pass.
Agents receive the selected worker and agent artifacts through the gateway.
API replicas keep serving the activated release throughout the sync. Only replicas
of the activated build authorize upgrades. Admission requires the target artifacts
or an explicitly compatible worker-image and agent-digest pair. Previous admission
does not establish compatibility. Reconnects still require source-cache activation
and a fresh request poll before placement.

Managed and joined agents update in place through their supervised service after
existing work drains. PostgreSQL retains the update intent across agent restarts
and Redis loss until the target worker accepts request polls. A managed pool first
obtains a current worker with room for the source's allocations. Compute resumes
a suitable reserve or provisions one temporary machine without increasing desired
capacity. It protects that capacity until the source updates and accepts work,
then idle retirement removes the excess. Platform maintenance runs one update at
a time within CPU and GPU fleets and yields to interruption recovery. Customer
pools retain their connection, quotas and purchase policy. Non-preemptible workloads
can delay an update until they finish.

`deploy/runtime-compatibility.json` is the reviewed list of exact runtime pairs
that the published release accepts alongside its target. It is empty by default.
Add a pair only after validating that runtime against the new control plane,
including dispatch, completion and cleanup. Do not infer compatibility from a
version number. An incompatible worker receives no new containers, function
invocations or endpoint requests, even when it is the only worker. A release with
no compatible serving capacity has an admission gap until current capacity is
ready. Breaking protocols require a bridge release that supports both sides
before removing the old protocol. The first rollout of these readers must complete
before publishing a nonempty compatibility list; older readers reject that field.

Platform stopped and hibernated reserves refresh under their admission hold, one
at a time while demand is clear, and count as current only after the provider
confirms the stop. Connected customer pools use Auto Scaling groups and do not
own stopped reserves. Attached machines update when reachable; a workload pinned
to one cannot move to another host. A missing supervisor or rejected agent update
is reported as blocked and does not reopen admission.

Run `lazycloud-admin release status` to check the rollout. It reports running and
offline machines, reserves, and pending capacity. `complete` stays false until
every machine is current and temporary capacity has retired. Argo health only
proves control-plane rollout.

Joined agents installed without the supervisor need `install-service` once before
automatic binary updates can run. Managed node installation already includes it.

To roll back, dispatch Deploy with the earlier complete manifest URL. Deployment
generations increase even when the selected version decreases.
Both PyPI projects accept the workflow through trusted publishing, configured on
each project as repository `AmbientWare/lazycloud`, workflow `ship.yml`,
environment `release`. No token is stored anywhere. The projects themselves were
created once by hand, because PyPI allows one pending publisher per workflow and
the two packages share one; ordinary publishers on existing projects have no
such limit.

See [release assets](aws-release-assets/README.md) for artifact validation
and [agent builds](agent-binary/README.md) for architecture selection.
The local API normally listens on port 8000; inspect the mapping with
`docker compose port control-plane 9000`.

### Recreating the control plane

```bash
docker compose up -d --build control-plane
docker compose ps control-plane connection-gateway connection-gateway-1 agent
```

Gateway sessions belong to the separate connection gateway processes. Inspect
the agent's session identity, the Redis connection owner, and both ends' logs if a
request stalls. See [connection gateway deployment](connection-gateway.md).

## Reading a failed node

In this order. Stop at the first step that answers the question.

### Read the recorded failure

```bash
docker compose exec -T postgres psql -U lazycloud -d lazycloud -x -c "
select i.instance_id, i.status,
       m.lifecycle, m.lifecycle_failure, m.lifecycle_message, m.lifecycle_at
from compute_provider_instances i
left join machines m on m.id = i.machine_id
order by i.created_at desc limit 5;"
```

The machine row carries the phase, the failure reason and the node's own
message. The reason identifies the failure category. Read detailed bootstrap logs only
in a private operator session; they may include sensitive data.

### Read recent events

```bash
docker compose exec -T postgres psql -U lazycloud -d lazycloud -x -c "
select created_at, action, level, resource_id, message
from events
where resource_type = 'provider-instance'
order by created_at desc limit 10;"
```

### Read EC2 console output

Console output can explain a failure before the agent starts. Use the
customer diagnostics role only for the designated acceptance account:

```bash
CREDS=$(AWS_PROFILE=default-test-source aws sts assume-role \
  --role-arn arn:aws:iam::<account>:role/lazycloud-default-test-diagnostics \
  --role-session-name diag --query Credentials --output json)
export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | jq -r .AccessKeyId)
export AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | jq -r .SecretAccessKey)
export AWS_SESSION_TOKEN=$(echo "$CREDS" | jq -r .SessionToken)

aws ec2 get-console-output --region us-east-1 --instance-id <i-...> --output text
```

### Inspect the running node through SSM

The node must be running and registered with SSM. Select its exact instance ID:

```bash
aws ssm describe-instance-information --region us-east-1 \
  --query 'InstanceInformationList[].[InstanceId,PingStatus]' --output text

aws ssm send-command --region us-east-1 \
  --document-name AWS-RunShellScript \
  --targets Key=InstanceIds,Values=<i-...> \
  --parameters commands='journalctl -u lazycloud-agent -n 200 --no-pager'
```

The diagnostics role is deliberately separate from the acceptance operator role:
`ssm:SendCommand` is remote code execution on a running node, scoped by the
`cloud-pool:managed-by=control-plane` launch tag. Assume it to diagnose, not to
run acceptance.

## Draining capacity

A node whose bootstrap failed holds its pool's slot until the controller
reclaims it at the bootstrap deadline. Do not terminate managed instances
directly; the control plane can replace them.

To remove a customer's connected AWS capacity, disconnect the account:

```bash
uv run --group workspace lazycloud cloud disconnect --wait
```

This drains capacity across every workspace the account backs and removes its
AWS authorization. Provisioning policy is defined in code; it has no customer
CLI override. Inspect current instances with `uv run --group workspace lazycloud compute instances`.

## Public ingress

`https://lazycloud.dev` reaches the origin through the `public-ingress`
connector. Routes are in `deploy/public-ingress/cloudflared.yml`, not the
dashboard; `deploy/public-ingress/README.md` owns mint and rotation.

Check connector health before changing edge routing or DNS:

```bash
docker compose exec -T control-plane python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:20241/ready',timeout=5).read().decode())"
```

`readyConnections` above zero means the connector is fine and the problem is at
the edge or in DNS. A `530`/`1033` with a healthy connector is DNS: the
hostname's record is not a proxied CNAME to this tunnel. Read the zone through
the API. `dig` cannot distinguish a flattened apex CNAME from an unrelated
proxied A record.

Repeated `control stream encountered a failure while serving` with every
network precheck passing means the tunnel no longer exists at Cloudflare, not a
connectivity fault.

## The hosted deployment

Creating or destroying a deployment is `deploy/platform-deployment/LIFECYCLE.md`.
This section is about running one that exists.

Every deployment runs on one EKS cluster declared by `deploy/platform-core`, in
a namespace named for it, with its own AWS resources declared by
`deploy/platform-deployment`. Nothing changes what a deployment runs except a
commit on its branch, and nothing writes that branch except the `Deploy`
workflow. Argo CD reconciles the namespace to the branch, so a deploy is a
commit and a rollback is a revert.

```sh
# Everything: cut a version, publish the package and the release, then record
# a build against it. Prod directly, until staging runs.
gh workflow run ship.yml -f bump=patch

# Select an already published complete release, including for rollback.
gh workflow run deploy.yml -f deployment=prod -f release_manifest_url=<manifest-url>

# Prod onto the commit and release staging runs. No build.
gh workflow run promote.yml

```

Deploy selects the supplied complete release. Ship supplies the URL automatically.
Release manifests are immutable; incomplete publication cannot advance the deployment.

For a manual full sync, first confirm that no operation is running. Stop if this
prints an operation:

```sh
kubectl -n argocd get application lazycloud-prod -o jsonpath='{.operation}'
```

Submit the sync and clear the completed operation state together. Argo can retain
a previous selective sync's resource filter and prune setting when merging the
next operation state. This command leaves pruning disabled and runs the full
chart, including its release activation hook:

```sh
kubectl -n argocd patch application lazycloud-prod --type merge \
  -p '{"status":{"operationState":null},"operation":{"sync":{"revision":"prod","prune":false}}}'
```

Check the migration, workloads, and active-release generation. A successful
selective sync does not prove that the release was activated.

Images are built once per commit into repositories every deployment shares,
so `Promote` finds every image already published and writes prod's values
file. What crosses from staging is one release URL; prod's
tunnel, secrets and database are rendered from prod's own infrastructure descriptor.

CI runs Helm validation and rendering, but only Argo installs workloads.

Worker-image releases update agents and workers in place.
Host AMI changes still require node replacement. Verify running workload survival
and placement latency during a release; Kubernetes readiness alone does not prove
either. Do not assume every image release should replace every managed node.

Watching a deploy:

```sh
aws eks update-kubeconfig --name lazycloud --region us-east-1
kubectl -n argocd get applications
kubectl -n lazycloud-prod get pods
```

### Reading a slow scheduler

The scheduler runs four loops in one process, each on its own cadence, and each
stamping its own heartbeat at `/tmp/lazycloud-scheduler.heartbeat.<loop>`. The
liveness probe reads all four, so a restart means one of them stopped finishing
passes, not that the process died.

Which one is the first thing to establish:

```sh
kubectl exec -n lazycloud-prod deploy/scheduler -- \
  sh -c 'for f in /tmp/lazycloud-scheduler.heartbeat.*; do echo "$f $(stat -c %Y "$f")"; done'
```

The oldest names the wedged loop. `placement` means containers are not being
decided on and callers are waiting; `capacity` means the fleet and its records
are drifting; `housekeeping` means an external service is unreachable and the
meter outbox is filling, which is durable and recoverable; `dispatch` means work
is decided but not placed.

To see what placement latency actually is, read the gap between a task becoming
claimable and starting, rather than inferring it from logs:

```sql
select name,
       round(avg(extract(epoch from (started_at - claimable_at)))::numeric, 1) as avg_s,
       round(max(extract(epoch from (started_at - claimable_at)))::numeric, 1) as max_s
from tasks
where started_at is not null and claimable_at is not null
group by name order by avg_s desc;
```

Compare placement delay with the same workload's baseline. Read the pending
reason and scheduler loop timings to distinguish compute startup from a
stalled scheduler.

### Spot capacity and replica placement

Auto Mode sizes capacity from pod requests and placement constraints. The
cluster-owned `platform-spot` NodePool admits compatible amd64 C/M/R Spot
instances across the existing subnets. It has no fixed size or node count.
Keep requests accurate; Auto Mode does not measure and rewrite them for us.

Requests are declared in three files, and they have to be read together because
they land on the same nodes:

| Workload | Declared in |
| --- | --- |
| control plane, scheduler, cache, tunnel, Jobs, once per deployment | `deploy/chart/values.yaml` |
| External Secrets (3 pods) | `deploy/argocd/apps/external-secrets.yaml` |
| Argo CD (7 pods) | `deploy/platform-core/argocd.tf` |

The API, scheduler, Cloudflare connectors and gateway each require two nodes
and two zones, including across revisions during a rollout. A replica may be
Pending while Auto Mode provisions. Inspect its scheduling events, NodeClaims,
NodeClass conditions and node availability together. A Spot shortage can affect
both replicas; disruption budgets cannot prevent provider reclamation.

Argo and External Secrets share Spot capacity. Auto Mode provisioning runs
outside these nodes, so their outage does not stop replacement provisioning.
The cache retains its single EBS claim and recovers in the disk's zone. See
[the migration and recovery checks](spot-capacity.md) before draining a node.

There is no metrics-server in this cluster, so `kubectl top` returns
`Metrics API not available`. Read usage from the kubelet through the API server
instead. It needs no install and no write:

```sh
kubectl get --raw "/api/v1/nodes/<node>/proxy/metrics/resource" \
  | grep -E 'container_(cpu_usage_seconds_total|memory_working_set_bytes)'
```

`container_cpu_usage_seconds_total` is a counter. Sample it twice and divide by
the wall time between the samples; a single reading is cumulative CPU time, not current utilization.

Probe timeouts across unrelated pods can indicate node contention. Compare
CPU, memory, scheduling events, and connector logs before changing one
workload's probes.

### Hosted service dependencies

`deploy/chart` defines the hosted processes. Images have immutable commit
tags and the release selects their executable digests.

| Not started | Served instead by |
| --- | --- |
| `postgres` | PlanetScale, through `LAZYCLOUD_DATABASE_URL` |
| `otel-collector` | whatever `LAZYCLOUD_TELEMETRY_ENDPOINT` names |
| `container-worker` | enrolled agents on managed or joined compute |
| `agent`, `agent-join-token` | a real joined machine |
| `redis` | ElastiCache, through `LAZYCLOUD_REDIS_URL`; a primary and a standby with automatic failover |

### The database connection string

Use `LAZYCLOUD_DATABASE_URL` on the transaction pooler at port 6432 for
application queries. Set `LAZYCLOUD_DATABASE_DIRECT_URL` to port 5432 for
migrations, administrator operations, and session advisory locks. Do not send
session locks through transaction pooling. See [connection budgets](CONFIGURATION.md#postgresql-connections).

### Database query and egress watch

Keep the provider's query-insights view and public-egress metric enabled for the
production database. Alert when either departs from the deployment's normal
hourly rate, and keep a hard alert at 50 GiB per day until ordinary customer
traffic justifies a higher reviewed limit. A scheduler that reads task or
container history every 250 ms can cross that limit before API latency changes.

On PostgreSQL installations the platform owns, preload and enable
`pg_stat_statements`. Rank statements by calls, rows, and total execution time
after each deployment. Placement reads must stay proportional to current stubs
and live containers, never to the number of tasks or containers retained in the
database.

### Adding an operator credential

Add its property binding to `secrets.map` in the chart and add its name only to
the consumers that need it under `environment`. Preserve the other properties
when updating the operator-managed Secrets Manager document. Deploy the chart
change. No Terraform apply is needed.

Do not print credentials to verify delivery. Inspect the ExternalSecret condition
and the container's key references, then exercise the credential's operation.
For rotation, refresh the Secret and bump only the affected `secretRevisions`.
See [configuration and pause procedures](CONFIGURATION.md).

### Schema changes

The schema moves by migration. A change to any table adds a revision under
`packages/database/src/database/alembic/versions/` chained onto the one before,
and `0001_initial` is frozen. A pull request that changes a model without adding
a revision fails on
`test_a_model_changed_without_a_revision_is_caught_here`, which builds a
database by running every revision and compares it to the metadata.

Argo runs `lazycloud-admin database migrate` as an ordered Sync-wave Job, after
secret projection and before any workload that reads the schema is updated. A failed migration
stops the sync with the running deployment untouched.

A rollback to an image older than the schema is refused rather than migrated
from, because there is no path to compute from a revision that build does not
carry. Roll forward, or restore the database.

### Provider commitment accounting upgrade

Revision `0015_provider_commitments` counts unresolved provider launches against
the fleet limit and backfills existing retained instance checkpoints. Stop all
old API and scheduler replicas before migration so they cannot create launches
without recording their commitments. Pause Argo automatic sync, preserve its
current settings, and confirm no active workloads before stopping those replicas.
Publish and select the release, run a full sync, verify the migration and new
replicas, then restore Argo's previous automatic sync settings. Keep PostgreSQL,
Redis, and worker instances intact. Verify fleet commitments alongside AWS
inventory before calling the warm and stopped targets healthy.

### Concurrent maintenance upgrade

Revision `0028_capacity_maintenance` moves active runtime replacements into
durable per-machine operations and drops the old release fields on
`compute_units`. Old API and scheduler processes cannot use the new schema.
Revision `0029_fleet_demand` adds the bounded arrival index.

Pause Argo automatic sync and record its current settings before publishing the
release. Keep the old controllers serving while images build. Once the deployment
branch selects the complete new manifest, confirm no active workloads or arrange
a service pause. Stop the old API and scheduler Deployments and any operator job
using the old compute models. Confirm those pods and their database sessions are
gone before running a full Argo sync. The migration must finish before new
controllers start. Keep databases, Redis, tunnel gateways and worker nodes intact.

Verify the schema revision, new controller images, migrated maintenance ownership
and fleet commitments against provider inventory. Restore the recorded automatic
sync settings. Do not roll old controller images back after this migration;
recovery must roll forward. This upgrade does not authorize deleting capacity or
resetting durable state.

### Capacity headroom upgrade

Revision `0023_capacity_headroom` drops `compute_units.warm_handoff_from`, lets
GPU units hold stopped reserves, adds the live-load index the reserve planner
reads, and creates `compute_node_shapes`, filled with the least memory enrolled
machines reported for each nominal CPU, memory and card count. Older API and
scheduler replicas read and write the dropped column, and an older scheduler
plans machine-count reserves against the same units, so both stop before the
migration. Pause Argo automatic sync and record its settings, scale the API and
scheduler Deployments to zero, then Ship and run a full sync. Confirm the
migration, then that one scheduler logs `platform reserve for` each market within
a minute. Restore Argo's automatic sync. Redis needs no clearing: the planner's
keys are new, and no stored hot-state model changed. Existing GPU Auto Scaling
groups keep serving and retire when idle; new GPU capacity is retained EC2.

### Agent directory release upgrade

The agent ships as a release archive unpacked once per digest, and the node
image masks the login banner and boot loader services and orders the agent
before Docker rather than after the network. An older agent
cannot update into the new layout, so every platform machine and stopped reserve
is replaced rather than upgraded.

1. Run the Node Images workflow on `main` for the CPU and GPU variants. Release
   reads the node image catalog, so it has to be current before Ship.
2. Pause Argo automatic sync on `root` and record its settings. Scale the
   scheduler to zero, so the planner does not resume or launch a machine on the
   old image while the fleet is replaced.
3. Terminate every platform machine and every stopped reserve through their
   groups and pools.
4. Ship. The release builds the agent archive. No migration runs.
5. Restore Argo's automatic sync and wait until `lazycloud-prod` reports the
   new revision Synced before scaling the scheduler back. Argo first re-syncs
   its cached revision, and a scheduler running then buys machines on the old
   image. The planner then launches fresh machines and reserves on the new one.
6. Machines customers joined and connected-cloud nodes run the old agent too.
   Run their join command again right after the Ship.

An agent from before this release cannot apply the archive as an update. The
gateway drains its machine, and the agent downloads the archive again on every
stream, until the machine is joined again.

IAM and Redis need no change. A node on the new image logs `docker answered
after` from its agent, and `df -h /` shows its whole root volume.

### Reserve hibernation upgrade

Reserves on instance types EC2 can hibernate stop with their worker running and
fenced. The agent and the control plane change their stream contract together,
and an older node image leaves acpid off, so EC2's hibernate request never
reaches it. The fleet is replaced again, the same way as the directory release
upgrade:
Node Images on `main`, pause Argo and scale the scheduler to zero, terminate
every platform machine and stopped reserve, Ship, then restore. Migration
`0025_reserve_hibernation` adds `compute_provider_instances.hibernates`.

IAM needs no change: `ec2:StopInstances` covers a hibernating stop, and the root
volume is encrypted with the account's default EBS key. Redis needs no clearing.

hibinit-agent writes `resume=PARTUUID=... resume_offset=...` to the boot entry
at a reserve's first cold boot, and the initrd finds the root partition by that
PARTUUID. On a reserve, `grep resume= /proc/cmdline` shows it from the second
boot on. Every cold boot logs `systemd-hibernate-resume@dev-disk-by-partuuid-...`
from the initrd, and a resume logs `PM: hibernation: hibernation exit`. Each
cold boot of a reserve also rebuilds its initrd, about ten seconds of CPU beside
the agent's start.

A reserve that hibernated logs `hibernating reserve` in the scheduler, and on
resume `starting reserve ..., stopped by Client.UserInitiatedHibernate`. Its agent
logs `machine slept for`, `agent tunnel connected` and `adopted reserve worker`,
each with its boot-relative time, and its worker logs `registration finished`.

### Python invocation format upgrade

Python invocation format 2 preserves Python object graphs and resolves function
dependencies through pickle references. Format 1 is rejected. JSON invocation
and result formats remain at version 1.

This alpha upgrade requires a coordinated cutover:

1. Stop function admission and scheduled invocations with the current release.
   Drain running calls, cancel queued calls, and stop their containers.
2. Inventory legacy Python task records by workspace and task ID. Their
   `invocation.encoding` is `cloudpickle` and `invocation.version` is `1`.
   Remove the reviewed records before starting the new application. This deletes
   their task history and results. Completed records also need removal because
   the new task reader validates the stored invocation format.
3. Deploy the API, scheduler, shared contracts, and runner from the same release.
   Upgrade the calling SDK before reopening admission and schedules. Restart
   existing function containers with the new runner.
4. Run `tests.e2e.local.function.scenario_serialization` against a local stack
   built from that release, then verify Python calls and dependency chains against
   the deployment before reopening it to callers.

Do not roll an old runner or SDK back into this deployment. Function result
storage is unchanged by this upgrade.

### Secrets

The External Secrets Operator reads them from Secrets Manager as the
deployment's `secrets-reader` service account, whose token it exchanges for the
`<deployment>-secrets-reader` role, and materialises one Kubernetes Secret the
workloads read by variable name. The role's trust admits that namespace alone,
so a staging store cannot read prod's documents. No credential is ever in the
chart or on the deployment branch: both are git, and a value committed there
outlives every rotation.

For rotation, preserve unrelated JSON properties in the operator document.
Refresh External Secrets and inspect its Ready condition and resource version.
Then bump the affected `secretRevisions` in the chart and deploy. Environment
variables and subPath mounts do not refresh in an existing pod. Verify an
authenticated operation before retiring the predecessor credential.

### Connecting the platform account to its own fleet

See [Provider provisioning](PROVIDERS.md) for the provisioning flow and
capacity lifecycle. The AWS-specific account registration below remains part
of normal deployment; it is not a separate scheduler path.

The `fleet-ensure` Job registers platform AWS networks and the connection
role through the API after the control plane starts. Terraform owns those
persistent resources. The scheduler owns the capacity launched through them.
Generate the connection-role policy from `provider_aws.connection_policy`.

Review the Terraform plan before expanding an existing fleet network. Existing
subnets and their CIDRs must remain unchanged; new AZs add subnets and route table
associations. Customer authorization stacks create up to six standard-AZ subnets
automatically and attach an S3 gateway endpoint to their route table. When
updating an existing customer stack, retain `AvailabilityZoneA` and
`AvailabilityZoneB` with `UsePreviousValue`, and assign the remaining discovered
zones to `AvailabilityZoneC` through `AvailabilityZoneF`. Review the change set
for additions without replacing existing subnets.

## Secrets and rotation

| Secret | Where it lives | Rotate by |
| --- | --- | --- |
| Tunnel issuer and gateway bootstrap credential | Local CA volume/private environment or production operator secret document | Bootstrap once with `deploy.tunnel_identity`; plan CA trust rotation separately. Leaf certificates renew automatically. |
| Cloudflare tunnel credentials | file named by `LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE` | Mint a second tunnel, repoint both DNS records, recreate `public-ingress`, then delete the old tunnel. see `deploy/public-ingress/README.md` |
| Cloudflare API token (operator and certificate renewal) | Operator environment `CLOUDFLARE_API_TOKEN`; existing operator secret document property `LAZYCLOUD_TCP_DNS_API_TOKEN` | The same token authenticates Terraform and cert-manager. On rotation, update both stored copies. The chart projects the renewal copy only into the certificate controller's Secret. It retains its existing Tunnel, DNS, Zone and certificate permissions. |
| Cloudflare API token (control plane) | `.env`, `LAZYCLOUD_CLOUDFLARE_API_TOKEN` | Reissue in the Cloudflare dashboard; scoped to Zone > SSL and Certificates > Edit. This is the one the control plane serves custom hostnames with. |
| Stripe webhook signing secret | `.env`, `LAZYCLOUD_STRIPE_WEBHOOK_SECRET` | Returned only when the endpoint is created. Replace the endpoint through `deploy/stripe`, take the new output, recreate `control-plane`. |
| Stripe API key | `.env`, `LAZYCLOUD_STRIPE_API_KEY` | Roll the restricted key in the Stripe dashboard, update `.env`, recreate `control-plane`. |
| Resend webhook secret | `.env`, `LAZYCLOUD_RESEND_WEBHOOK_SECRET` | Returned only when the endpoint is created. Re-register `POST /webhooks/resend` in the Resend dashboard, take the new secret, recreate `control-plane`. Mail keeps sending without it; only delivery reporting stops. |
| Resend API key | `.env`, `LAZYCLOUD_RESEND_API_KEY` | Read by the scheduler, which sends the mail; the control plane only queues it. Create a new key in the Resend dashboard, update `.env`, recreate `scheduler`, then delete the old key. Queued messages are durable, so mail queued during the gap goes out after it. |

## Irreversible actions

Confirm the target belongs to the task before each of these. None can be undone.

- Deleting the tunnel issuer key. Existing certificates stop renewing.
  Preserve the issuer until a planned trust rotation reaches every agent.
- Deleting a customer connection stack. Removes the roles the control plane
  assumes; the connection must be re-established from scratch.
- Deleting launch-template versions. The pool cannot roll back to a template
  version that no longer exists.

## Telemetry

Configure the collector's OTLP backend and credentials through the deployment's
secret bindings. Inspect collector errors and confirm metrics arrive at the
backend after a rollout. A healthy API does not prove telemetry delivery.

Use query latency, placement delay, task failures, and gateway reconnects to
set alerts against measured normal traffic. Workers report container metrics
through the worker repository.
