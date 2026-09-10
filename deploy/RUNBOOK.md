# Operator Runbook

Commands for starting, inspecting, and recovering a deployment.

## Bring-up

```bash
cd /path/to/lazycloud
uv run --frozen --group workspace python -m deploy.release
```

The command builds the Compose images, reports service health, and activates the
release after the platform is healthy.

### Billing, before the first sign-in

An installation that will charge anybody needs both of these before its first
customer arrives. Signing in provisions a subscription and fails closed if it
cannot, and a subscription resolves its prices by lookup key, so an account whose
catalog is unpublished refuses **every** sign-in it receives, not only the ones
that would have been billed. Usage is priced as it is recorded, so a window
metered before the rates exist is written down as unpriced and charged nothing,
and the deployment says so several times a second in `billing.span.unpriced`.

A cluster deployment runs both from the chart on every sync, as the
`billing-catalog` and `billing-rates` Jobs in
`deploy/chart/templates/bootstrap-jobs.yaml`. The commands below are the same
work for the Compose stack, which has no Argo to run them.

```bash
# Which account, and which kind of account. Dry run first: without --confirm
# this only reads, and reports `live_mode` alongside what is missing.
uv run lazycloud-admin billing publish-catalog --confirm-account acct_...
uv run lazycloud-admin billing publish-catalog --confirm-account acct_... --confirm

# Rates, at or before the first billable second. The dry run attempts the write
# and rolls it back, so it answers whether the boundary would be accepted.
uv run lazycloud-admin billing publish-rates
uv run lazycloud-admin billing publish-rates --confirm
```

Both are additive and idempotent, and both refuse rather than edit when what is
already published disagrees. Running the rates twice at the same instant writes
nothing the second time and says `already_published` for each rate, which is what
lets the Job repeat. There is no un-publish for either, because a rate boundary
is a figure customers are charged either side of.
`LAZYCLOUD_STRIPE_WEBHOOK_SECRET` must be set before the first card is saved. The
endpoint refuses every delivery without it, and Stripe disables endpoints that
keep failing.

Usage metered before the rates were published stays unpriced. Nothing revisits
it, so charge it with a window you name:

```bash
uv run lazycloud-admin billing price-unpriced --from <iso> --to <iso>
uv run lazycloud-admin billing price-unpriced --from <iso> --to <iso> --confirm
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
Managed hosts replace through their existing controller. Joined agents need the
supervised `install-service` installation once to support automatic binary updates.
An older agent without the update protocol also needs that initial upgrade.
Existing work drains; long-running pods remain and can delay replacement.

To roll back, dispatch Deploy with the earlier complete manifest URL. Deployment
generations increase even when the selected version decreases.
Both PyPI projects accept the workflow through trusted publishing, configured on
each project as repository `AmbientWare/lazycloud`, workflow `ship.yml`,
environment `release`. No token is stored anywhere. The projects themselves were
created once by hand, because PyPI allows one pending publisher per workflow and
the two packages share one; ordinary publishers on existing projects have no
such limit.

`--arch` defaults to `amd64`, which is what AWS node classes consume. Building
`arm64` needs binfmt registered first (`docker run --privileged tonistiigi/binfmt
--install arm64`), or the cross-architecture stage fails with `exec format
error`.

### Checking the deployment

The command checks that `control-plane` and `scheduler` loaded the selected
control, worker and host URLs and agree on `LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL`.
Disagreement fails the check.

It then prints each host's booted launch-template version beside the pool's
recorded target, polling for a post-restart observation. Missing or stale host
versions fail the report. This is host inventory, not proof that workers switched
images or that workloads survived. Verify the target worker image on enrolled
slots and run the public workload acceptance before declaring rollout complete.

`--skip-restart` publishes and writes the pins without restarting the local
stack. It skips the running-process and host checks.

The API answers on host port **8000** (container port 9000). `docker compose port
control-plane 9000` prints the mapping if it changes.

### Recreating the control plane

```bash
docker compose up -d --build control-plane wireguard-platform
docker compose ps control-plane wireguard-platform tunnel-gateway
```

`wireguard-platform` shares the control plane's network namespace. Recreate both
services together or the sidecar remains attached to the namespace of the old
container. `tunnel-gateway` is separate and should remain healthy throughout.

If an agent is unreachable, read the handshake state at both ends before
restarting either one. A healthy process without a recent handshake has not
proved the private route.

## Reading a failed node

In this order. Stop at the first step that answers the question.

**1. The durable reason.** Always start here; it named every failure this
platform has had.

```bash
docker compose exec -T postgres psql -U lazycloud -d lazycloud -x -c "
select instance_id, status,
       payload->>'bootstrap_phase'          as phase,
       payload->>'bootstrap_failure_reason' as reason,
       payload->>'bootstrap_failure_detail' as detail
from compute_provider_instances
order by created_at desc limit 5;"
```

`reason` is a closed enum; `detail` is the excerpt the node sent with it. Treat
`detail` as sensitive — a bootstrap log can carry a credential.

**2. The durable event**, which carries the same excerpt and survives the
instance row:

```bash
docker compose exec -T postgres psql -U lazycloud -d lazycloud -x -c "
select created_at, action, level, resource_id, message
from events
where resource_type = 'provider-instance'
order by created_at desc limit 10;"
```

**3. Console output.** The only diagnostic that survives a node which never
reached userland — and it works on terminated instances.

```bash
CREDS=$(AWS_PROFILE=default-test-source aws sts assume-role \
  --role-arn arn:aws:iam::<account>:role/lazycloud-default-test-diagnostics \
  --role-session-name diag --query Credentials --output json)
export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | jq -r .AccessKeyId)
export AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | jq -r .SecretAccessKey)
export AWS_SESSION_TOKEN=$(echo "$CREDS" | jq -r .SessionToken)

aws ec2 get-console-output --region us-east-1 --instance-id <i-...> --output text
```

**4. A shell on the node**, via SSM. Requires the node to be running and its
agent healthy enough to have registered with SSM.

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
uv run lazycloud cloud disconnect --wait
```

This drains capacity across every workspace the account backs and removes its
AWS authorization. Provisioning policy is defined in code; it has no customer
CLI override. Inspect current instances with `uv run lazycloud compute instances`.

## Public ingress

`https://lazycloud.dev` reaches the origin through the `public-ingress`
connector. Routes are in `deploy/public-ingress/cloudflared.yml`, not the
dashboard; `deploy/public-ingress/README.md` owns mint and rotation.

Separate connector health from edge routing before anything else — the two
fail identically from outside:

```bash
docker compose exec -T control-plane python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:20241/ready',timeout=5).read().decode())"
```

`readyConnections` above zero means the connector is fine and the problem is at
the edge or in DNS. A `530`/`1033` with a healthy connector is DNS: the
hostname's record is not a proxied CNAME to this tunnel. Read the zone through
the API — `dig` cannot distinguish a flattened apex CNAME from an unrelated
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

# Neither: make Argo reconcile now rather than on its next poll.
kubectl -n argocd patch application lazycloud-prod --type merge \
  -p '{"operation":{"sync":{"revision":"prod"}}}'
```

Deploy selects the supplied complete release. Ship supplies the URL automatically.
Release manifests are immutable; incomplete publication cannot advance the deployment.

Images are built once per commit into repositories every deployment shares,
so `Promote` finds every image already published and writes prod's values
file. What crosses from staging is one release URL; prod's
tunnel, secrets and database are rendered from prod's own infrastructure descriptor.

CI runs Helm validation and rendering, but only Argo installs workloads.

Worker-image releases use the in-place worker-agent rollout introduced in #134.
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
kubectl exec -n lazycloud deploy/scheduler -- \
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

Before the loops were split this averaged 30s with a 918s worst case, because
placement ran behind a synchronous Stripe drain in the same tick. A warm
container answers in about 0.1s; anything in seconds is a cold start, and
anything in tens of seconds is a loop that is not running.

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
[the migration and recovery checks](SPOT_PLAN.md) before draining a node.

There is no metrics-server in this cluster, so `kubectl top` returns
`Metrics API not available`. Read usage from the kubelet through the API server
instead. It needs no install and no write:

```sh
kubectl get --raw "/api/v1/nodes/<node>/proxy/metrics/resource" \
  | grep -E 'container_(cpu_usage_seconds_total|memory_working_set_bytes)'
```

`container_cpu_usage_seconds_total` is a counter. Sample it twice and divide by
the wall time between the samples; a single reading is a lifetime average and
hides everything that matters.

The failure this guards against does not look like a resource problem from
outside. A starved node reports its requests at 75% while its CPU sits at 85%,
every process keeps running, and what the user sees is Cloudflare returning 502
because `cloudflared` could not run for long enough to answer a QUIC keepalive.
Liveness probes timing out with `context deadline exceeded` across unrelated
pods at once is the signal. One workload failing its own probe is that
workload's problem; four failing together is the node.

### What a deployment runs, and what it does not

`deploy/chart` is what a deployment runs, and it is not the local `compose.yaml`
with pieces removed -- it is the same processes declared for a cluster. Images
carry the tag of the commit that built them, which is safe because every ECR
repository is created with immutable tags.

| Not started | Served instead by |
| --- | --- |
| `postgres` | PlanetScale, through `LAZYCLOUD_DATABASE_URL` |
| `otel-collector` | whatever `LAZYCLOUD_TELEMETRY_ENDPOINT` names |
| `container-worker` | the connected-AWS pool the scheduler launches |
| `agent`, `agent-join-token` | a real joined machine |
| `platform-unit`, `worker-token` | not needed; those feed the Compose fleet |
| `redis` | ElastiCache, through `LAZYCLOUD_REDIS_URL`; a primary and a standby with automatic failover |

### The database connection string

Use the **direct endpoint on 5432**, never PgBouncer. PlanetScale's managed
PgBouncer is transaction-pooling only, and `ControlPlaneRecoveryFence` holds
`pg_advisory_lock_shared` for the lifetime of a serving process. Behind a
transaction pooler that lock is released when the backend is recycled, the fence
stops fencing **without erroring**, and offline recovery can mint an
administrator credential while replicas are still serving.

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

### Secrets

The External Secrets Operator reads them from Secrets Manager as the
deployment's `secrets-reader` service account, whose token it exchanges for the
`<deployment>-secrets-reader` role, and materialises one Kubernetes Secret the
workloads read by variable name. The role's trust admits that namespace alone,
so a staging store cannot read prod's documents. No credential is ever in the
chart or on the deployment branch: both are git, and a value committed there
outlives every rotation.

To rotate one, write the new value. Nothing else is needed -- the operator
refreshes on its interval and the pods pick it up:

```sh
aws secretsmanager put-secret-value --secret-id lazycloud-prod/<name> --secret-string '<value>'
```

A workload that caches a credential at startup needs a restart to notice, which
is a property of that process rather than of the rotation:

```sh
kubectl -n lazycloud-prod rollout restart statefulset/control-plane
```

### Connecting the platform account to its own fleet

See [Provider provisioning](PROVIDERS.md) for the common AWS/Hetzner flow and
capacity lifecycle. The AWS-specific account registration below remains part
of normal deployment; it is not a separate scheduler path.

Shared capacity is a connected-AWS pool in the platform's own account, using the
same managed flow a customer uses, which is what the control stack anticipates
when it says a customer account can be this account. `deploy/platform-deployment`
declares the fleet VPC, a subnet in each available standard AZ, the security group and the connection
role, one set per deployment, and the `fleet-ensure` Job registers them through
the public API after the control plane is serving. The connection role's policy
is never hand-written: it is rendered from `provider_aws.connection_policy` into
`connection-role-policy.json`, and CI fails on a stale copy.

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
| WireGuard gateway and platform keys | Local `wireguard-keys` volume or production `<deployment>/wireguard` Secrets Manager document | Do not hand-rotate. Replacing the gateway identity invalidates enrolled peer configurations; perform a scoped deployment reset or a planned re-enrollment instead. |
| Cloudflare tunnel credentials | file named by `LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE` | Mint a second tunnel, repoint both DNS records, recreate `public-ingress`, then delete the old tunnel — see `deploy/public-ingress/README.md` |
| Cloudflare API token (operator) | operator shell only, `CLOUDFLARE_API_TOKEN` | Reissue in the Cloudflare dashboard; scoped to Tunnel:Edit, DNS:Edit, Zone:Read. **Not a deployment value** — nothing in the stack reads it and it is absent from `.env.example`. It authenticates `deploy/cloudflare` and hand-run API calls. |
| Cloudflare API token (control plane) | `.env`, `LAZYCLOUD_CLOUDFLARE_API_TOKEN` | Reissue in the Cloudflare dashboard; scoped to Zone > SSL and Certificates > Edit. This is the one the control plane serves custom hostnames with. |
| Stripe webhook signing secret | `.env`, `LAZYCLOUD_STRIPE_WEBHOOK_SECRET` | Returned only when the endpoint is created. Replace the endpoint through `deploy/stripe`, take the new output, recreate `control-plane`. |
| Stripe API key | `.env`, `LAZYCLOUD_STRIPE_API_KEY` | Roll the restricted key in the Stripe dashboard, update `.env`, recreate `control-plane`. |
| Resend webhook secret | `.env`, `LAZYCLOUD_RESEND_WEBHOOK_SECRET` | Returned only when the endpoint is created. Re-register `POST /webhooks/resend` in the Resend dashboard, take the new secret, recreate `control-plane`. Mail keeps sending without it; only delivery reporting stops. |
| Resend API key | `.env`, `LAZYCLOUD_RESEND_API_KEY` | Read by the scheduler, which sends the mail; the control plane only queues it. Create a new key in the Resend dashboard, update `.env`, recreate `scheduler`, then delete the old key. Queued messages are durable, so mail queued during the gap goes out after it. |

Legacy credentials from the superseded architecture live outside the repo at
`~/.lazycloud-legacy-secrets/secrets-backup/`. They are **not** rotated. Anything
still live there (AWS keys, Cloudflare, WorkOS, Polar, Resend, Depot, Upstash,
Prefect) should be rotated and the directory deleted.

## Irreversible actions

Confirm the target belongs to the task before each of these. None can be undone.

- **Deleting the WireGuard key document or local key volume.** This changes the
  gateway identity and invalidates existing peer configurations. Reset all
  related local state together, or use a planned production re-enrollment.
- **Deleting a customer connection stack.** Removes the roles the control plane
  assumes; the connection must be re-established from scratch.
- **Deleting launch-template versions.** The pool cannot roll back to a template
  version that no longer exists.
- **`ssm:SendCommand`.** Arbitrary code on a running customer node.

## Not yet covered

- **Alerting.** A deployment now exports to a real backend:
  `deploy/telemetry/collector.deploy.yaml` replaces the debug exporter with an
  OTLP one, and the collector holds the backend credential because
  `TelemetrySettings` has no headers field — a process can push OTLP but cannot
  authenticate to a hosted backend. Set `telemetry-backend-endpoint`,
  `-username` and `-password` in Secrets Manager.

  What is still missing is what the numbers should mean. Alert thresholds come
  after there is history to read them against.

  Only `control-plane` and `scheduler` export. They are the two processes that
  call `setup_telemetry`, and between them they record every platform metric —
  the container worker publishes its container metrics through the worker
  repository instead, on a path that does not use the meter.

  A missing telemetry credential is deliberately not fatal. The collector cannot
  export and says so; the control plane keeps serving, because an exporter that
  cannot reach a receiver drops the batch rather than failing the process.
