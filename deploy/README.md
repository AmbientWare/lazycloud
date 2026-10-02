# Production deployment

Terraform builds the infrastructure (deploy/terraform), Ship builds and
publishes a version, Deploy records it on the `prod` branch, and Argo CD
syncs the chart (`deploy/helm/lazycloud`) from that branch. CI never runs
Terraform or Helm against a cluster.

## What runs

Namespace `lazycloud-prod` on an EKS Auto Mode cluster (Kubernetes 1.36):

| Workload | Image | Reached through |
| --- | --- | --- |
| server (2) | `lazycloud/release/server:<version>`, carrying the agent archive of that version | cloudflared for the apex paths `/v1`, `/auth`, `/webhooks`, `/install`, for `*.<domain>` and for customer domains (edge); the `server-hosts` NLB, TLS 443 terminated with an ACM certificate for `hosts.<domain>`, ALPN h2, plaintext gRPC to the pods; the `server-tcp` NLB, TLS passthrough on 1995 with a cert-manager `*.tcp.<domain>` certificate |
| scheduler (2) | `lazycloud/release/scheduler:<version>` | nothing |
| web (2) | `lazycloud/release/web:<version>`, the static build behind unprivileged nginx | cloudflared for every other apex path |
| cloudflared (2) | `cloudflare/cloudflared` by digest | the deployment's locally managed tunnel |
| migrate Job | server image | Argo Sync hook, wave -1 |
| publish-agent-release Job | server image | Argo PostSync hook |
| otel-collector | contrib, off by default | `telemetry.enabled` |

Argo waves: secrets-reader and SecretStore (-4), ExternalSecret (-3), TCP
Issuer and Certificate (-2), migrations (-1), workloads (0), agent release
(PostSync). Shared operators are External Secrets (v1 API only),
cert-manager and Argo CD, pinned in deploy/argocd/apps and platform-core.

NetworkPolicy is enforced: platform-core sets
`enable-network-policy-controller` in kube-system's `amazon-vpc-cni`
ConfigMap. The NodeClass keeps `DefaultAllow`, so only pods a policy selects
are restricted, and the chart's policies deny ingress to every release pod
except from named peers. Telemetry stays off until a backend is chosen;
turning it on takes the backend's endpoint, two operator keys for its basic
auth and `telemetry.enabled`.

### Database

PostgreSQL is PlanetScale Neki: database `lazycloud-prod`, branch `main`,
reached through the router on 5432 with `sslmode=verify-full`. The router
pools backends, so there is no client-side pooler; each process bounds its
pgx pool with `pool_max_conns=8`. PostgreSQL is the only durable store.

Connections that keep session state open from
`LAZYCLOUD_DATABASE_SESSION_URL` (`database.OpenSession`): every LISTEN (the
server's and scheduler's listeners, the dashboard's change hub, the edge's
route watch), the migration lock, the metering lock and the scheduler's
leader lock. The leader checks `pg_locks` for its own lock on every check, so
a session a proxy moved to another backend cannot pass for the leader.
Everything else is a transaction or a single statement: the other advisory
locks are `pg_advisory_xact_lock`, nothing runs `SET`, and pgx's cached
prepared statements use the extended protocol. Neki has no endpoint besides
its router, so both URLs name the router. Behind a pooler that drops session
state, point the session URL at a direct endpoint.

The server migrates at start, so its role needs schema rights. Migrations
only go forward.

### Node image

Platform and connected-account hosts launch from the baked node image
(`deploy/ami`, the Node images workflow), shared with each connected account
before its first launch. A region without an image fails the host instead
of launching one without gVisor. The image and its copies are unencrypted so
they can be shared; they hold public software only, and every host encrypts
its root volume at launch. A connected account keeps launch permission after
it disconnects; each bake makes new images that start unshared.

The image is Amazon Linux 2023 with Docker, gVisor `runsc` as Docker's
`runsc` runtime with `--host-uds=all` (plus `--nvproxy` on GPU images), the
nbd module at boot, and qemu-storage-daemon, qemu-img and nbd-client built
from pinned sources. A systemd drop-in sets `LAZYCLOUD_OCI_RUNTIME=runsc` for
the agent. `deploy/host-pins.sh` holds the pins; `deploy/local/host-setup.sh`
reads the same gVisor pin. Agent archives are linux/amd64 only.

## Settings and secrets

"Values" is platform-deployment's `values.json` in the deploy bucket, which
Deploy merges into `values-deployment.yaml`. "Env file" is
`deploy/helm/lazycloud/environments/prod.yaml`. "Chart" means the chart sets
it and refuses it in config. `deploy/check.sh` fails when the chart gives a
container a `LAZYCLOUD_*` variable its binary does not read.

Secrets Manager `lazycloud-prod/platform` (us-east-1), written whole by
Terraform:

| Property | Becomes | Read by |
| --- | --- | --- |
| `LAZYCLOUD_DATABASE_URL` | the same | server, scheduler, migrate, publish-agent-release |
| `LAZYCLOUD_DATABASE_SESSION_URL` | the same | server, scheduler, migrate |
| `LAZYCLOUD_SECRETS_MASTER_KEY` (32 random bytes, `prevent_destroy`) | file `secrets.key`, mode 0440, `LAZYCLOUD_SECRETS_KEY_FILE` | server, scheduler |
| `LAZYCLOUD_STRIPE_WEBHOOK_SECRET` (from the Terraform-made endpoint) | the same | server |
| `LAZYCLOUD_CLOUDFLARE_TUNNEL_CREDENTIALS` | file `credentials.json` | cloudflared |

`lazycloud-prod/operator`, which the operator writes and Terraform only
reads. platform-deployment fails if it is missing. The chart maps these
properties and ignores any others:

| Property | Read by |
| --- | --- |
| `LAZYCLOUD_GITHUB_CLIENT_ID`, `LAZYCLOUD_GITHUB_CLIENT_SECRET` (GitHub App OAuth) | server |
| `LAZYCLOUD_STRIPE_API_KEY` | server, scheduler |
| `LAZYCLOUD_RESEND_WEBHOOK_SECRET` | server |
| `LAZYCLOUD_RESEND_API_KEY` | scheduler |
| `LAZYCLOUD_CLOUDFLARE_API_TOKEN` (SSL and Certificates edit, custom domains) | server |
| `LAZYCLOUD_TCP_DNS_API_TOKEN` (DNS edit on the zone) | cert-manager's DNS-01 solver |

Server, beyond those:

| Variable | Source |
| --- | --- |
| `HTTP_ADDR` 8080, `GRPC_ADDR` 8081, `EDGE_ADDR` 8082, `EDGE_RELAY_ADDR`/`_ADVERTISE` pod IP:8083, `EDGE_TCP_ADDR` 8084, `HEALTH_ADDR` 8090, `METRICS_ADDR` 9090, `DRAIN_DELAY` 5s | chart |
| `PUBLIC_URL`, `EDGE_URL` `https://<domain>`; `EDGE_TCP_URL` `tls://tcp.<domain>:1995`; `EDGE_TCP_CERT`/`_KEY` the certificate's secret; `AGENT_SERVER_ADDR` `hosts.<domain>:443`; `INSTALL_URL`; `CLIENT_RELEASE_VERSION` the release version | chart |
| `AGENT_DIST_DIR` `/opt/lazycloud/agent-dist` | image |
| `OBJECT_STORE_REGION`, `OBJECT_STORE_BUCKET`, `WORKSPACE_BUCKET_PROVIDER` aws, `_PREFIX`, `_ROLE_ARN` | values |
| `IMAGE_REGISTRY` (the account's ECR), `IMAGE_REPOSITORY` `lazycloud/workload-images`, `IMAGE_REGISTRY_HOST_ROLE_ARN` (the role host logins are scoped from), `CLOUDFLARE_ZONE_ID` | values |
| `FLEET_NAME`, `FLEET_ACCOUNT_ID`, `FLEET_NODE_ROLE_ARN`, `FLEET_INSTANCE_PROFILE`, `FLEET_NETWORKS`, `AWS_PRINCIPAL_ARN` | values |
| `FLEET_IMAGES`, `FLEET_MAX_HOSTS`, `FLEET_IDLE_TIMEOUT`, `FLEET_HEADROOM`, `LOG_FORMAT` | env file |
| `OTLP_ENDPOINT`, `OTLP_INSECURE`, `TRACE_SAMPLE_RATIO` | chart, telemetry on |
| `AWS_REGION` | values; credentials from Pod Identity |
| unset: object store endpoint and keys, `GRPC_TLS_*`, `IMAGE_REGISTRY_USERNAME`/`_PASSWORD` (ECR tokens come from Pod Identity), `IMAGE_TEMPLATE`, `GARAGE_*`, `AWS_ENDPOINT_*` | |

The scheduler takes health, metrics and the master key from the chart,
`AGENT_SERVER_ADDR` and `INSTALL_URL` (written into instance user data) from
the chart, and the object store, workspace buckets and fleet as the server
does. `RESEND_FROM` defaults to `LazyCloud <noreply@lazycloud.dev>`.

The agent on fleet hosts gets `--server hosts.<domain>:443`, `--gateway`,
`--cloud-host-id`, `--agent-version` and `--agent-sha256` from the
launcher's user data, `LAZYCLOUD_OCI_RUNTIME=runsc` from the image and IMDS
for cloud identity, and verifies TLS against ACM's public chain. Joined
machines get the same server address in their join command.

## Health and drain

Both binaries serve probes on `LAZYCLOUD_HEALTH_ADDR` (8090), a port no
Service exposes.

- Server: `/healthz` answers while the process serves; `/readyz` reports
  `draining` once SIGTERM arrives and leaves the database out, so a database
  outage does not pull every replica from the load balancer. The server
  keeps serving for `LAZYCLOUD_DRAIN_DELAY`, then ends host sessions and
  gives requests a 10 s grace.
- Scheduler: `/readyz` is true once every loop finished a pass; `/healthz`
  fails when a loop has not beaten within its interval plus 2 minutes. Each
  pass runs under a 1 minute deadline. On SIGTERM a pass in flight rolls
  back and another replica or the next start repeats it. Exactly one
  replica logs `leading timed passes`.

## Bring-up

Run every apply from an operator machine with `AWS_PROFILE=default` after
`aws sts get-caller-identity` names the platform account. A fresh account
needs, before step 1:

- the state bucket and backend file (deploy/terraform/README.md);
- Secrets Manager `lazycloud-prod/operator` in us-east-1 with the properties
  above;
- the account's GitHub OIDC provider, which the roots read;
- a PlanetScale organization enrolled in Neki, Cloudflare for SaaS on for
  the zone, and the release reviewers' GitHub user ids.

1. Apply `platform-core` with `cluster_api_cidrs` set to the operator's
   address.
2. Apply `platform-deployment` (`deployment`, `github_environment`,
   `domain`, `cloudflare_account_id`, `cloudflare_zone_id`,
   `planetscale_organization`). Check the Neki profile size in the
   dashboard.
3. Check that the router keeps session state. Open two `pscale shell
   lazycloud-prod main` sessions, A and B.
   - LISTEN: A runs `LISTEN lc_check;` and a few `SELECT 1;`. B runs
     `NOTIFY lc_check, 'b';`. A's next statement prints B's notification.
   - Session lock: A runs `SELECT pg_advisory_lock(42);` and a few `SELECT
     1;`. B's `SELECT pg_try_advisory_lock(42);` returns false. A's `SELECT
     pg_advisory_unlock(42);` returns true, so A's later statements ran on
     the backend that took the lock. B's try then returns true.
   - Repeat both after A has sat idle for five minutes.

   If a check fails, LISTEN and the session locks need a direct endpoint;
   stop there.
4. Apply `github` (`release_reviewer_user_ids`).
5. Run Node images and commit the `LAZYCLOUD_FLEET_IMAGES` value from its
   summary into the env file through a reviewed PR.
6. Run Ship. A reviewer approves the image push; Ship publishes the CLI to
   PyPI and Deploy writes `prod`. Argo CD syncs.
7. Apply `platform-deployment` again with `host_load_balancer` and
   `tcp_load_balancer` from `kubectl -n lazycloud-prod get svc server-hosts
   server-tcp`.
8. Create the first administrator with `kubectl -n lazycloud-prod exec
   deploy/server -- /usr/local/bin/server admin create-user --email <owner>
   --admin`, sign in with GitHub and provision billing through the service.
9. Verify the dashboard and sign-in; `lazycloud login`; a function on
   platform compute; an endpoint on its generated host; a TCP pod; an image
   build pushing to ECR; `machine join`; a function in a connected AWS
   account; a custom domain; a Stripe delivery; an invitation email; Argo CD
   at `argocd.<domain>`; one scheduler logging `leading timed passes`; and
   `kubectl -n lazycloud-prod get policyendpoints` listing one per
   NetworkPolicy.

After a release, server and scheduler logs show no `prepared statement`
errors. A `database listener disconnected` warning at most every 30 minutes
on a quiet platform is the router's `idle-session-timeout`; the listener
reconnects and re-reads.

## Rollback

`gh workflow run deploy.yml -f version=<earlier>` records an earlier
version and Argo CD syncs it. Migrations do not roll back, so the earlier
version runs against the newer schema.

## Known limits

- The TCP certificate renews 30 days before expiry, but the server reads it
  at start, so a renewal takes effect at the next release or restart.
- A presigned multipart upload fails if the server's Pod Identity
  credentials expire mid-upload; its `expires_at` says when.
- Image builds use Docker's `bridge` network.
