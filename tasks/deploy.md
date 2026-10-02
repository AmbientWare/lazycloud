# Deploy packet

go-rewrite holds everything needed to build the new platform fresh in the
platform account, whose old infrastructure is gone: Terraform
roots, Argo CD applications, the chart, the images, the node image bake and
the Ship, Deploy and Node images workflows. Nothing here has touched AWS,
GitHub, PlanetScale, Cloudflare or Stripe. Every step below marked [GO]
waits for the user.

## What runs

Namespace `lazycloud-prod` on a new EKS Auto Mode cluster (Kubernetes
1.36), synced by Argo CD from the `prod` branch (`deploy/helm/lazycloud`):

| Workload | Image | Reached through |
| --- | --- | --- |
| server (2) | `lazycloud/release/server:<version>`, carrying the agent archive of that version | cloudflared for the apex paths `/v1`, `/auth`, `/webhooks`, `/install` and for `*.<domain>` and customer domains (edge); `server-hosts` NLB, TLS 443 terminated with an ACM certificate for `hosts.<domain>`, ALPN h2, plaintext gRPC to the pods; `server-tcp` NLB, TLS passthrough on 1995 with a cert-manager `*.tcp.<domain>` certificate |
| scheduler (2) | `lazycloud/release/scheduler:<version>` | nothing |
| web (2) | `lazycloud/release/web:<version>`, the static build behind unprivileged nginx | cloudflared for every other apex path |
| cloudflared (2) | `cloudflare/cloudflared:2026.9.3` by digest | the deployment's locally managed tunnel |
| migrate Job | server image | Argo Sync hook, wave -1 |
| publish-agent-release Job | server image | Argo PostSync hook |
| otel-collector | contrib 0.161.0, off by default | `telemetry.enabled` |

Argo waves: secrets-reader and SecretStore (-4), ExternalSecret (-3), TCP
Issuer and Certificate (-2), migrations (-1), workloads (0), agent release
(PostSync). Shared operators: External Secrets 2.11.0 (v1 API only),
cert-manager v1.21.2, Argo CD chart 10.9.6 (Argo CD 3.5.3).

PostgreSQL is PlanetScale Neki: database `lazycloud-prod`, branch `main`,
default configuration profile (one shard, a primary and two replicas) and
router group, reached through the router on 5432 with `sslmode=verify-full`.
The router pools backends, so there is no PgBouncer and no client-side
pooler; each process bounds its pgx pool with `pool_max_conns=8`. There is
no Redis.

Fleet hosts launch from the baked node image (`deploy/ami`): Amazon Linux
2023 with Docker, gVisor `runsc` 20260928 as Docker's `runsc` runtime with
`--host-uds=all` (plus `--nvproxy` on GPU images, driver 590.48.01, which
that gVisor release proxies), the nbd module at boot, and qemu-storage-daemon
9.2.3, qemu-img and nbd-client 3.26.1 built from pinned sources because
Amazon Linux packages neither the daemon nor the client. A systemd drop-in
sets `LAZYCLOUD_OCI_RUNTIME=runsc` for the agent. `deploy/host-pins.sh`
holds the pins; `deploy/local/host-setup.sh` reads the same gVisor pin.

## Settings and secrets

"Values" is platform-deployment's `values.json` in the deploy bucket, which
Deploy merges into `values-deployment.yaml`. "Env file" is
`deploy/helm/lazycloud/environments/prod.yaml`. "Chart" means the chart sets
it and refuses it in config.

`lazycloud-prod/platform`, written whole by Terraform:

| Property | Becomes | Read by |
| --- | --- | --- |
| `LAZYCLOUD_DATABASE_URL` | the same | server, scheduler, migrate, publish-agent-release |
| `LAZYCLOUD_SECRETS_MASTER_KEY` (32 random bytes, `prevent_destroy`) | file `secrets.key`, mode 0440, `LAZYCLOUD_SECRETS_KEY_FILE` | server, scheduler |
| `LAZYCLOUD_STRIPE_WEBHOOK_SECRET` (from the Terraform-made endpoint) | the same | server |
| `LAZYCLOUD_CLOUDFLARE_TUNNEL_CREDENTIALS` | file `credentials.json` | cloudflared |

`lazycloud-prod/operator`, which the operator owns and Terraform only looks
up. It already holds these from main, unchanged:

| Property | Read by |
| --- | --- |
| `LAZYCLOUD_GITHUB_CLIENT_ID`, `LAZYCLOUD_GITHUB_CLIENT_SECRET` (GitHub App OAuth) | server |
| `LAZYCLOUD_STRIPE_API_KEY` | server, scheduler |
| `LAZYCLOUD_RESEND_WEBHOOK_SECRET` | server |
| `LAZYCLOUD_RESEND_API_KEY` | scheduler |
| `LAZYCLOUD_CLOUDFLARE_API_TOKEN` (SSL and Certificates edit, custom domains) | server |
| `LAZYCLOUD_TCP_DNS_API_TOKEN` (DNS edit on the zone) | cert-manager's DNS-01 solver |

The document also keeps main's `LAZYCLOUD_STRIPE_WEBHOOK_SECRET` (its
endpoint is gone; the platform document's replaces it),
`LAZYCLOUD_ADMINISTRATOR_GITHUB_USER_ID` and three `LAZYCLOUD_TUNNEL_*`
properties of main's connection gateway. The chart maps none of them.

Server, beyond those:

| Variable | Source |
| --- | --- |
| `HTTP_ADDR` 8080, `GRPC_ADDR` 8081, `EDGE_ADDR` 8082, `EDGE_RELAY_ADDR`/`_ADVERTISE` pod IP:8083, `EDGE_TCP_ADDR` 8084, `HEALTH_ADDR` 8090, `METRICS_ADDR` 9090, `DRAIN_DELAY` 5s | chart |
| `PUBLIC_URL`, `EDGE_URL` `https://<domain>`; `EDGE_TCP_URL` `tls://tcp.<domain>:1995`; `EDGE_TCP_CERT`/`_KEY` the certificate's secret; `AGENT_SERVER_ADDR` `hosts.<domain>:443`; `INSTALL_URL`; `CLIENT_RELEASE_VERSION` the release version | chart |
| `AGENT_DIST_DIR` `/opt/lazycloud/agent-dist` | image |
| `OBJECT_STORE_REGION`, `OBJECT_STORE_BUCKET`, `WORKSPACE_BUCKET_PROVIDER` aws, `_PREFIX`, `_ROLE_ARN` | values |
| `IMAGE_REGISTRY` (the account's ECR), `IMAGE_REPOSITORY` `lazycloud/workload-images`, `CLOUDFLARE_ZONE_ID` | values |
| `FLEET_NAME`, `FLEET_ACCOUNT_ID`, `FLEET_NODE_ROLE_ARN`, `FLEET_INSTANCE_PROFILE`, `FLEET_NETWORKS`, `AWS_PRINCIPAL_ARN` | values |
| `FLEET_IMAGES`, `FLEET_MAX_HOSTS`, `FLEET_IDLE_TIMEOUT`, `FLEET_HEADROOM`, `LOG_FORMAT` | env file |
| `OTLP_ENDPOINT`, `OTLP_INSECURE`, `TRACE_SAMPLE_RATIO` | chart, telemetry on |
| `AWS_REGION` | values; credentials from Pod Identity |
| unset: object store endpoint and keys, `GRPC_TLS_*`, `IMAGE_REGISTRY_USERNAME`/`_PASSWORD` (ECR tokens from Pod Identity), `IMAGE_TEMPLATE` (default python slim), `GARAGE_*`, `AWS_ENDPOINT_*` | |

Scheduler: health, metrics and master key from the chart; `AGENT_SERVER_ADDR`
and `INSTALL_URL` (written into instance user data) from the chart; object
store, workspace buckets and fleet as for the server; `RESEND_FROM` defaults
to `LazyCloud <noreply@lazycloud.dev>`.

Agent on fleet hosts: `--server hosts.<domain>:443`, `--gateway`,
`--cloud-host-id`, `--agent-version` and `--agent-sha256` from the launcher's
user data; `LAZYCLOUD_OCI_RUNTIME=runsc` from the image; IMDS by default for
cloud hosts; paths from the release bundle; TLS against ACM's public chain.
Joined machines get the same server address in their join command.

`deploy/check.sh` fails when the chart gives a container a `LAZYCLOUD_*`
variable its binary does not read.

## Main against the port

Lines counted without generated lock files and main's 5.2k lines of
managed-runtime wheel locks.

| Piece | main | port | What changed and why it is better |
| --- | ---: | ---: | --- |
| Terraform | 4,478 (platform-core, platform-deployment, cloudflare, stripe, terraform-state docs) | 1,737 (state, platform-core, platform-deployment, github, README) | Four roots for one deployment become one: Cloudflare tunnel and records, Stripe webhook and the deployment's AWS resources share one apply, so the webhook secret and tunnel credentials go straight into the platform document instead of being copied by hand, and the descriptor publishing step is an `aws_s3_object`. Dropped Redis, release CloudFront and bucket, access-log bucket and queue, control principal, fleet connection role and its 357-line generated policy, acceptance roles, moved blocks. The control-plane policy grants the calls the binaries make: no bucket policy, logging or deletion rights, no Secrets Manager, EC2 limited to RunInstances/TerminateInstances on fleet-tagged instances. Every provider pinned exactly to its current release; helm and kubernetes providers move to 3.x. A state root replaces a hand-made bucket. |
| Database | PlanetScale Postgres branch, PgBouncer on 6432 plus a direct URL | PlanetScale Neki branch and one role, router on 5432 | One URL; the router pools. |
| Chart | 2,670 (chart, values renderer) | 1,570 (chart, schema, prod environment, example values) | Seven Python services, connection gateway with HAProxy sidecar, cache server, fleet controller and bootstrap jobs become server, scheduler, web and cloudflared. Values are Terraform's JSON merged with the version by jq; `chart_values.py` and its 367 lines go. One disruption template for all four workloads. |
| Argo CD | 155 | 141 | Same three applications at current versions; the deployment reads `deploy/helm/lazycloud`. |
| Workflows | 908 (ship, promote, deploy, build-platform, release, node-images, deployment-definition) | 413 (ship, deploy, node-images, deploy-checks) | Ship builds with one bake call and records with Deploy. Release manifests, the previous-release reuse planner and S3 asset publication go: the image tag is the version and the server carries its agent. Promote went with staging. Deploy takes a version, so a rollback is `gh workflow run deploy.yml -f version=<old>`. |
| Images | 775 (control-plane, worker, agent Dockerfiles and bake) | 235 | One Dockerfile; no worker image. |
| Node images | 1,548 (bake.py, catalog.py, recipe) | 273 | A shell bake and recipe; the image map is reviewed into the env file instead of a published catalog. |
| Release, ingress and connected-AWS tooling | 9,898 including runtime locks (release.py, aws-release-assets, agent-binary, managed-runtime, connection gateway, tunnel identity, PgBouncer, connected-aws scripts) | 0 | The agent bundle builds in the image; the connection template lives in compute. |
| Docs | 1,455 (README, RUNBOOK, CONFIGURATION, PROVIDERS, spot-capacity) | 295 (this file, deploy/AGENTS.md) | |
| Checks | none local | 101 (`deploy/check.sh`) | Lint, render, schema, env, Terraform, actionlint and shellcheck in one script CI also runs. |
| Total | 17,519 | 4,765 | |

## Bring-up runbook

Main's infrastructure was destroyed on 2026-10-01: the EKS cluster, VPCs,
Redis, buckets, the Cloudflare tunnel and records, the Stripe webhooks and
the PlanetScale database. Everything below is a fresh build. Kept:

- Secrets Manager `lazycloud-prod/operator` (us-east-1), which the new
  platform reads as is.
- The state bucket `lazycloud-terraform-state-534742592531`; main's state
  objects in it are empty.
- The tailnet states, which are not part of this stack.
- Stripe's one live customer, on a $0 subscription.
- The account's GitHub OIDC provider, which the roots read.

`lazycloud-prod/platform` was force-deleted, so Terraform creates it anew.

1. [GO] Prerequisites: the PlanetScale organization joins the Neki
   Platform Preview (an organization administrator, in the dashboard);
   Cloudflare for SaaS is on for the zone; the release reviewers' GitHub
   user ids are known.
2. [GO] The integrator merges go-rewrite into main, so Argo CD's root
   Application finds the new applications when it starts.
3. [GO] `state` apply; write the backend file from its output.
4. [GO] `platform-core` apply (`cluster_api_cidrs` with the operator's
   address).
5. [GO] `platform-deployment` apply (`deployment`, `github_environment`,
   `domain`, `cloudflare_account_id`, `cloudflare_zone_id`,
   `planetscale_organization`). It reads `lazycloud-prod/operator` and
   fails if the document is missing. Check the Neki profile size in the
   dashboard.
6. Verify Neki before shipping: in two `pscale shell lazycloud-prod main`
   sessions run `LISTEN lc_test` and `NOTIFY lc_test`, and `SELECT
   pg_advisory_lock(1)` in one while the other's `pg_try_advisory_lock(1)`
   returns false. The platform depends on both. If either fails, switch
   database.tf back to `planetscale_postgres_branch` (GA) before going on.
7. [GO] `github` apply (`release_reviewer_user_ids`).
8. [GO] Run Node images; commit the `LAZYCLOUD_FLEET_IMAGES` value from its
   summary into the env file through a reviewed PR.
9. [GO] Run Ship with `bump: major`. A reviewer approves the image push; it
   publishes the CLI to PyPI and Deploy writes `prod`. Argo CD syncs.
10. [GO] `platform-deployment` apply again with `host_load_balancer` and
    `tcp_load_balancer` from `kubectl -n lazycloud-prod get svc server-hosts
    server-tcp`.
11. [GO] Create the first administrator: `kubectl -n lazycloud-prod exec
    deploy/server -- /usr/local/bin/server admin create-user --email <owner>
    --admin`, sign in with GitHub, provision billing through the service.
12. Verify: dashboard and sign-in; `lazycloud login`; a function on platform
    compute (a baked host in us-east-2 enrolls over `hosts.<domain>`); an
    endpoint on its generated host; a TCP pod; an image build pushing to
    ECR; `machine join`; a custom domain; a Stripe delivery; an invitation
    email; Argo CD at `argocd.<domain>`.

Rollback after bring-up is Deploy with an earlier version.

## Proposed shared changes

- `Propose: launch platform hosts from the baked node image of their region`
  (compute): `LAZYCLOUD_FLEET_IMAGES`; a region without an image fails the
  host instead of launching one without gVisor. Connection hosts keep the
  stock images. Tests:
  `TestPlatformHostsLaunchFromTheBakedNodeImageOfTheirRegion`,
  `TestPlatformHostsWithoutANodeImageForTheirRegionFail`.
- `Propose: log in to an ECR platform registry with refreshed tokens from the
  server's AWS identity` (images, hostsession, server). ECR tokens last 12
  hours, so a static login could not work. Test:
  `TestPlatformECRLoginIsMintedAndReplacedBeforeItExpires`.
- shellcheck fixes in `deploy/local/build-runtime.sh` and
  `garage-bootstrap.sh`; `host-setup.sh` reads the shared gVisor pin.

## Verification run

- `deploy/check.sh` and `./check.sh` pass: helm 4.3.0 lint `--strict` and
  render of prod and a variant (telemetry on, no TCP, cluster-internal
  hosts, existing Secret); five settings that must refuse to render do;
  kubeconform 0.8.0 `-strict` on Kubernetes 1.36.0 with External Secrets,
  cert-manager and Argo CD schemas; the env check; terraform 1.16.4 fmt,
  init with the committed locks and validate on all four roots; actionlint
  1.7.12; shellcheck 0.11.0.
- `docker buildx bake release` built server (192 MB, 110 MB of it the agent
  archive), scheduler (65 MB) and web (57 MB). The web image served `/`,
  prerendered pages, the app shell and `/healthz` read-only as uid 65532.
  The server image ran `migrate` and `admin publish-agent-release` against a
  scratch database and served `/install/agent/linux/amd64` with the
  published digest.
- The node image's package, qemu, nbd and gVisor steps ran in an
  `amazonlinux:2023` container. systemd, the module, the GPU driver and the
  console marker need a real bake.
- `go test -race` for internal/compute, internal/images,
  internal/hostsession and cmd/server.

Not run: any plan against real state, apply, destroy, cluster install, Argo
sync, External Secrets projection, ECR push, workflow run, AMI bake, EC2
launch or Neki connection.

## Open questions and gaps

- Neki is a Platform Preview ("Beta Features", no SLA) and needs an
  enrolled organization and an HA profile (three Postgres nodes, three
  routers), which costs more than a single PS_10 node. Its documented
  limits do not affect the schema (no temporary tables, `SELECT INTO`,
  `INTERSECT`/`EXCEPT`, large objects or `COPY` over the extended
  protocol), and session advisory locks route on a single shard. LISTEN
  through the router is not documented either way; step 15 checks it. The
  docs give no Postgres major version; the dashboard shows it. The
  provider's `access_host_url` is used as the host, as for Postgres roles;
  confirm on the first apply.
- The server migrates at start, so its role inherits `postgres`. A
  data-only role for the server and scheduler (`pg_read_all_data`,
  `pg_write_all_data`) would need migrations to run only in the Job.
- Connected AWS accounts launch stock Amazon Linux: runc and no disk tools.
  Sharing the baked images with connected accounts belongs to compute. The
  connection template's `LaunchTagged` statement also requires the fleet tag
  on network interfaces and Spot requests, which the launcher does not tag;
  real IAM may refuse those launches.
- Builds use the `bridge` network; the images packet wanted a registry-only
  network in production.
- NetworkPolicies render but Auto Mode enforces them only when the node
  class enables network policy, which platform-core does not do yet.
- Telemetry has no backend (main had none either); choose one, add its
  endpoint and two operator keys, set `telemetry.enabled`.
- The TCP certificate renews 30 days before expiry, but the server reads it
  at start, so a renewal waits for the next release or restart.
- Billing defect: `charge.refund.updated` delivers a Refund that
  `processEvent` retrieves as a Charge; the endpoint does not subscribe to
  it.
- Agent archives are linux/amd64 only, as on main.
