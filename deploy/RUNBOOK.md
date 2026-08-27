# Operator Runbook

What you cannot derive from the code. Every command here was run against a live
stack on 2026-07-31; where a section covers work that has not landed, it says so
rather than guessing.

## Bring-up

```bash
cd /path/to/lazycloud
COMPOSE_PROFILES='*' docker compose build      # all source-bearing images together
docker compose up -d
until [ "$(docker compose ps --format '{{.Health}}' control-plane | head -1)" = healthy ]; do sleep 5; done
```

Build **every** source-bearing image in one command. Building a subset produces
a package-digest mismatch that the managed runtime rejects at container start,
and the error names the digest rather than the stale image.

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
uv run lazycloud-admin billing publish-rates --effective-at 2026-01-01T00:00:00Z
uv run lazycloud-admin billing publish-rates --effective-at 2026-01-01T00:00:00Z --confirm
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

Do not perform the sequence by hand. It was written down here first and was
still performed wrong — two images rebuilt out of nine, and the mismatch
surfaced an hour later on a running EC2 node.

```bash
uv run python deploy/release.py \
  --bucket "$AWS_RELEASE_ASSET_BUCKET" \
  --worker-repository <registry>/lazycloud-container-worker \
  --cpu-ami us-east-1=ami-<id> \
  --gpu-ami us-east-1=ami-<id>
```

It refuses a dirty tree, because a version label that names a revision the
artifacts do not contain is worse than no label. It builds every image and the
agent executable from that one revision, publishes the worker image and the
release, and points `.env` at the published manifest.

`.env` receives one line: `LAZYCLOUD_RELEASE_MANIFEST_URL`. The agent artifact
version and digest, the URL serving it, the container-worker image, the customer
authorization template, and both baked AMI catalogs are read from that manifest
at startup. They were six copied variables until a deployment held five from one
release and one from another; there is now no second place for them to disagree.

`--cpu-ami` names the base image managed nodes boot. It is not optional for a
deployment that runs managed capacity: a release naming no AMI produces a control
plane that refuses to start rather than a pool that launches nothing.

`--gpu-ami` names the GPU node image from the same bake, and is optional — a
release without one simply offers no GPU instance types, and CPU capacity is
unaffected. Supply it and every GPU type in the catalog becomes launchable in that
region, because one image serves every card: the driver branch is unified across
Turing through Blackwell.

Neither flag is verified against AWS during a release. `verify` can check that the
AMIs exist, but only when run with `--aws-cli-verify`, which neither this command
nor the workflow passes; the recorded ids are validated by pattern alone.

`--arch` defaults to `amd64`, which is what AWS node classes consume. Building
`arm64` needs binfmt registered first (`docker run --privileged tonistiigi/binfmt
--install arm64`), or the cross-architecture stage fails with `exec format
error`.

### Did it reach the nodes

Publishing replaces no running instance. An Auto Scaling group whose launch
template moves v1→v2 leaves every InService node on v1 by design, so the last
thing the command does is say which release each node is actually running, and
exit non-zero when that is not this one.

First it compares the two processes that each compose a pool's bootstrap —
`control-plane` and `scheduler` — over the variables the release publishes plus
`LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL`, read from the environment each container
actually holds. While those disagree the launch template alternates on every
reconcile and no node settles on either version, so nothing said about nodes
afterwards would mean anything. Both holding the *previous* release fails here
too: it is the shape a restart that did not take leaves behind.

Then, per pool, it prints every node's booted launch-template version against
the pool's current one and repeats the reading until the record has been
rewritten by a reconcile newer than the restart — up to three minutes, since
the pooled reconcile runs on a 60s timer. Rows print every cycle whether or not
anything is stale; a report that prints only problems reads the same as one that
failed to look.

It only reads, and only the platform's own durable record: the control plane
owns the writes that would replace a node (see **Draining capacity**), and
re-querying EC2 would test EC2 rather than the release. The pool's current
launch-template version is the one value with no HTTP surface — it lives in the
pool's provider state — so both halves of the comparison come from one query at
one instant. A version the record cannot supply fails the report rather than
passing it.

`--skip-restart` skips the guard and the report along with the restart. The
release is published but this stack has not loaded it, so every node would read
as stale when nothing is wrong, and the closing line says published rather than
live.

The API answers on host port **8000** (container port 9000). `docker compose port
control-plane 9000` prints the mapping if it changes.

### Recreating the control plane

```bash
docker compose up -d --build control-plane
```

Nothing else needs recreating. The control plane runs its own `tailscaled` and no
service shares its network namespace, so a rebuild cannot leave anything attached
to a namespace that no longer exists.

It mints a short-lived, single-use key for a durable device when it starts. A
graceful shutdown logs that device out before stopping `tailscaled`. A hard kill
can leave a stale device in the tailnet, which must be removed by its exact
device identity.

A restarted daemon can hold a stale netmap that lists deleted devices as online
and omits new ones. If a node is on the tailnet but unreachable from the control
plane, restart `control-plane` before investigating further.

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

A node whose bootstrap failed keeps running and holds the pool's slot. The
reclaim path handles the deadline case; to clear a pool by hand, drive the
connected account's compute configuration to zero rather than terminating
instances directly — the control plane owns those writes and will otherwise
relaunch. This zeroes capacity in every workspace that account backs.

```bash
uv run lazycloud cloud compute update \
  --initial-cpu-workers 0 --min-cpu-workers 0 --max-cpu 0
```

Expect a `409` while a reconcile is in flight; retry. Watch it drain with
`uv run lazycloud compute instances`.

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

Creating or destroying a deployment is `deploy/platform-eks/LIFECYCLE.md`. This
section is about running one that exists.

The control plane runs in an EKS cluster declared by `deploy/platform-eks`.
Nothing changes what it runs except a commit on the deployment branch, and
nothing writes that branch except the `Deploy` workflow. Argo CD reconciles the
cluster to the branch, so a deploy is a commit and a rollback is a revert.

```sh
# Everything: publish a release, then record a build against it. A `v*` tag does
# the same thing without the dispatch.
gh workflow run ship.yml -f deployment=lazycloud-prod

# Code only, onto the release the deployment already runs.
gh workflow run deploy.yml -f deployment=lazycloud-prod

# Neither: make Argo reconcile now rather than on its next poll.
kubectl -n argocd patch application lazycloud --type merge \
  -p '{"operation":{"sync":{"revision":"prod"}}}'
```

`Deploy` on its own publishes no release. It reads the one the deployment already
names from `s3://<deploy bucket>/current/release-manifest-url` and carries it
forward, so shipping a code change does not take the fleet's managed capacity
away. That file is written only by a run that published a release, and the deploy
warns rather than proceeding quietly when there is none to read.

Nothing here runs `helm`. A workflow that installs and a controller that
reconciles are two opinions about what should be running, and they disagree where
nobody is looking.

Expect a ship to replace every managed node. A new release moves each pool's
launch template, and the scheduler drains the superseded machines onto it one at
a time, surging a replacement before it cordons anything. Nothing is lost, but
the fleet is briefly one node larger per pool.

Watching a deploy:

```sh
aws eks update-kubeconfig --name lazycloud-prod --region us-east-1
kubectl -n argocd get applications
kubectl -n lazycloud get pods
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

### Capacity, and why the cluster holds two nodes

Auto Mode sizes the cluster from what the pods request, and from nothing else.
There is no node group to grow and no instance type to pick. The way to give
this deployment more machine is to request accurately; the way to give it less
is the same.

Requests are declared in three files, and they have to be read together because
they land on the same nodes:

| Workload | Declared in |
| --- | --- |
| control plane, scheduler, cache, tunnel, Jobs | `deploy/chart/values.yaml` |
| External Secrets (3 pods) | `deploy/argocd/apps/external-secrets.yaml` |
| Argo CD (7 pods) | `deploy/platform-eks/argocd.tf` |

Two nodes is the floor and it is deliberate. `control-plane` and `cloudflared`
each spread one replica per node with `DoNotSchedule`, so a second replica
cannot share a node with the first. During bring-up, a rollout, or a node
replacement that replica sits `Pending` for around a minute while Karpenter
provisions. That is the constraint doing its job. `Pending` is the only state
Karpenter provisions for, so a spread that could be satisfied by packing would
never ask for the node.

A node vanishing from `kubectl get nodes` until only one remains is not
consolidation working. It means something started requesting less than it uses.

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
| `object-store`, `object-store-bucket` | S3, through the platform role |
| `otel-collector` | whatever `LAZYCLOUD_TELEMETRY_ENDPOINT` names |
| `container-worker` | the connected-AWS pool the scheduler launches |
| `agent`, `agent-join-token` | a real joined machine |
| `platform-unit`, `worker-token` | not needed; those feed the Compose fleet |

Redis stays on the host. It is also the reason there is one host: two would need
it moved to ElastiCache first, because it holds the leases the scheduler
serialises capacity work on.

### The database connection string

Use the **direct endpoint on 5432**, never PgBouncer. PlanetScale's managed
PgBouncer is transaction-pooling only, and `ControlPlaneRecoveryFence` holds
`pg_advisory_lock_shared` for the lifetime of a serving process. Behind a
transaction pooler that lock is released when the backend is recycled, the fence
stops fencing **without erroring**, and offline recovery can mint an
administrator credential while replicas are still serving.

### Secrets

The External Secrets Operator reads them from Secrets Manager as itself, through
a Pod Identity association, and materialises one Kubernetes Secret the workloads
read by variable name. No credential is ever in the chart or on the deployment
branch: both are git, and a value committed there outlives every rotation.

To rotate one, write the new value. Nothing else is needed -- the operator
refreshes on its interval and the pods pick it up:

```sh
aws secretsmanager put-secret-value --secret-id lazycloud-prod/<name> --secret-string '<value>'
```

A workload that caches a credential at startup needs a restart to notice, which
is a property of that process rather than of the rotation:

```sh
kubectl -n lazycloud rollout restart deployment/control-plane
```

### Connecting the platform account to its own fleet

Shared capacity is a connected-AWS pool in the platform's own account, using the
same managed flow a customer uses — which is what the control stack anticipates
when it says a customer account can be this account. The connection stack creates
the fleet VPC, its two subnets, the security group and the node instance profile,
and the control plane reads them back from the stack outputs.

`deploy/platform-eks` therefore declares no fleet network. Adding one there would
mean declaring the connection role beside it, and that role's policy is generated
in `provider_aws/account_connection.py`.

## Secrets and rotation

| Secret | Where it lives | Rotate by |
| --- | --- | --- |
| Tailscale OAuth client | Production operator secret or local `.env`, `LAZYCLOUD_TAILNET_OAUTH_CLIENT_*` | Each Tailnet Terraform state owns a different runtime client. Change its tag list and apply, then transfer both replacement outputs to that environment. Never put production's client in local `.env`. |
| Cloudflare tunnel credentials | file named by `LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE` | Mint a second tunnel, repoint both DNS records, recreate `public-ingress`, then delete the old tunnel — see `deploy/public-ingress/README.md` |
| Cloudflare API token (operator) | operator shell only, `CLOUDFLARE_API_TOKEN` | Reissue in the Cloudflare dashboard; scoped to Tunnel:Edit, DNS:Edit, Zone:Read. **Not a deployment value** — nothing in the stack reads it and it is absent from `.env.example`. It authenticates `deploy/cloudflare` and hand-run API calls. |
| Cloudflare API token (control plane) | `.env`, `LAZYCLOUD_CLOUDFLARE_API_TOKEN` | Reissue in the Cloudflare dashboard; scoped to Zone > SSL and Certificates > Edit. This is the one the control plane serves custom hostnames with. |
| Stripe webhook signing secret | `.env`, `LAZYCLOUD_STRIPE_WEBHOOK_SECRET` | Returned only when the endpoint is created. Replace the endpoint through `deploy/stripe`, take the new output, recreate `control-plane`. |
| Stripe API key | `.env`, `LAZYCLOUD_STRIPE_API_KEY` | Roll the restricted key in the Stripe dashboard, update `.env`, recreate `control-plane`. |

Legacy credentials from the superseded architecture live outside the repo at
`~/.lazycloud-legacy-secrets/secrets-backup/`. They are **not** rotated. Anything
still live there (AWS keys, Cloudflare, WorkOS, Polar, Resend, Depot, Upstash,
Prefect) should be rotated and the directory deleted.

## Irreversible actions

Confirm the target belongs to the task before each of these. None can be undone.

- **Deleting a tailnet device.** The control plane's own device name is sticky to
  the device record: delete it and it rejoins under a `-1` suffix, and every
  configured origin naming the old name breaks. See `deploy/AGENTS.md`.
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
