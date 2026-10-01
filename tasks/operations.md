# Operations packet

Parity sections: "Operations and administration" and "Local development, CI
and deployment" in update.md, plus the dashboard's admin settings Users API.
Migration 0012 is allocated and unused: nothing here needs schema.

## Scope decisions (user)

- The reference's internal admin CLI (`lazycloud-admin`, R/apps/cli) is not
  rebuilt. It was unused. This is an intentional difference.
- The dashboard admin settings stay. This packet builds the administrator
  Users API. Complimentary billing belongs to the billing packet and the
  Fleet view to the compute packet.
- An operator command stays only if something uses it. The server keeps these
  subcommands, all used by `deploy/local/run.sh`:
  - `server migrate`: run.sh, and the chart's pre-upgrade Job.
  - `server admin create-user`, `create-workspace`, `create-token`: run.sh
    bootstrap, and the first administrator of a cluster (see Deploying).
  - `server admin create-join-token`: run.sh enrolls its agent with it.

Reference admin operations that are not rebuilt, by where the capability now
lives, if anywhere:

- Billing packet: `billing publish-rates` (with an effective time),
  `publish-catalog`, `price-unpriced`, `user set-complimentary` and the
  credit grant. These are the only ones its runbooks used regularly.
- Compute packet: host drain and retire (fleet spin-down), `fleet destroy`,
  `fleet clear-plan`, `release status`, `release fetch-agent`, `machine
  join-token` (covered by `create-join-token`), `worker`/`unit`/`agent`
  inspection.
- Observability packet: `maintenance prune-logs` (log retention).
- Dropped as unused operator tooling: `user create/list/set-role/set-status`
  (the dashboard Users page replaces them), `token create/list/revoke` for
  other users, `profile export`, `workspace configure`, `platform
  initialize`, `database check/status/wait`, `auth bootstrap/recover`,
  `stub`, `concurrency`, `container`, `queue`, `map`, `object`, `cache`,
  `image build/list`, `cron list/delete/runs`, `scheduler tick/run/
  startup-latency/dispatch-containers/autoscaler *`, `usage list/summary`,
  `economics report`, `invoke`, `events`.

## Administrator Users API

`internal/identity/admin.go`, `internal/api/admin.go`, tag `admin` in
contracts/openapi.yaml.

- `GET /v1/users` (`listUsers`): `search` (display name, email or GitHub
  login, case-insensitive, LIKE wildcards literal, at most 200 characters),
  `role` (administrator|member), `status` (active|disabled), `limit` 1 to
  200 (default 50), `cursor`. Returns `{users, next_cursor}`, oldest first.
- `PUT /v1/users/{user}/role` (`setUserRole`) and `PUT /v1/users/{user}/status`
  (`setUserStatus`) return the `User`, which gains `status`.
- Members get 403, and so does an administrator's workspace-restricted token.
- Differences from the reference, both fixes. The server refuses changing
  your own role or status with 409; the reference only refused removing the
  last active administrator, and two administrators demoting each other at
  once could leave none. The change locks both rows and commits only while
  the caller is still an active administrator. Disabling revokes API tokens,
  deletes browser sessions and expires approved device codes in one
  transaction; the reference revoked tokens only. Enabling restores none.
- Billing reuse: `identity.ListUsers(ctx, p, UserQuery)` returns a
  `UserPage`; `/v1/billing/accounts` can call it and load plan and cost for
  the returned ids in one query.
- Web packet operations: `listUsers`, `setUserRole`, `setUserStatus`. The
  reference read `/api/v1/billing/accounts` with `data`/`next` and `role`;
  this reads `users`/`next_cursor` and `is_admin`, plus billing's fields once
  that packet lands.

Tests: TestListUsersFiltersAndPages, TestAccountAdministrationNeedsAnAdministratorAccount,
TestDisablingEndsCredentials, TestConcurrentMutualDemotionKeepsAnAdministrator
(internal/identity), TestAccountAdministrationOverHTTP (internal/api).

## Health and drain

- Both binaries serve probes on `LAZYCLOUD_HEALTH_ADDR` (port 8090 in the
  images and chart), a port no Service exposes.
- Server: `/healthz` answers while the process serves; `/readyz` reports
  `draining` once SIGTERM arrives. Readiness leaves the database out, so a
  database outage does not pull every replica from the load balancer. The
  server keeps serving for `LAZYCLOUD_DRAIN_DELAY` (default 0, chart 5 s),
  then ends host sessions and gives requests the 10 s shutdown grace. The
  NOTIFY listener and the token-use writer run on their own context, which
  ends only after the HTTP and gRPC servers stop, so waits and claims open
  during the drain still wake and token use is still recorded.
- Scheduler: `/readyz` is true once every loop finished a pass; `/healthz`
  fails when a loop has not beaten within its interval plus 2 minutes. Each
  pass runs under a 1 minute deadline and may report progress as a beat, so
  slow work ends the pass instead of restarting the pod; only a call that
  ignores its deadline stalls a loop. On SIGTERM the loops stop. A pass in
  flight is cancelled and its transaction rolls back; another replica or
  the next start repeats it. The storage sweep runs through the same loop,
  so `Storage.RunSweeper` is gone.
- Workspace deletion deletes at most 1000 objects per workspace per pass with
  batched DeleteObjects and reruns the pass at once while objects remain.
  Before, one pass deleted every object one request at a time, so a large
  workspace outlived the stall limit and liveness restarted every replica in
  turn.
- Version: `-ldflags -X main.version=` stamps all three binaries; server and
  scheduler log it at start, the agent reports it in its session.

Tests: TestServeDrainsOnShutdown (cmd/server; fails if token use stops at
drain start), TestSchedulerReadinessAndShutdown, TestHeartbeatsReportStalledLoops
and TestPassesEndAtTheirDeadlineAndProgressBeats (cmd/scheduler),
TestDeleteWorkspaceObjectsIsBoundedPerCall (internal/storage, 1001 objects). In the built images: `docker stop` of the server answered
503 on `/readyz` for 3 s with `LAZYCLOUD_DRAIN_DELAY=3s` and exited 0 after
3047 ms; the scheduler image became ready, then exited 0 on SIGTERM.

## Images

`deploy/images/Dockerfile` and `deploy/images/docker-bake.hcl`
(`docker buildx bake -f deploy/images/docker-bake.hcl`). Every base, the
dockerfile frontend and uv are pinned by digest. Builds use CGO_ENABLED=0,
-trimpath and -buildvcs=false, and bake rewrites timestamps to
SOURCE_DATE_EPOCH. `Dockerfile.dockerignore` sends only Go sources, Python
packages, the lock and the two scripts, so `.env` and local state never
enter a build.

- server (34.1 MB) and scheduler (18.9 MB): distroless static, nonroot.
- agent (324 MB): the release bundle, `/opt/lazycloud/bin/{agent,supervisor,
  geesefs}` and `/opt/lazycloud/runtimes/3.10` to `3.14` built by
  `deploy/local/build-runtime.sh`. The agent passes these paths to Docker,
  so hosts install the bundle at the same paths; running it as a container
  needs them mounted identically. Disk tools (qemu-storage-daemon, nbd-client,
  e2fsprogs) are host packages and are not in the image.

Reproducibility: two builds of one commit, the second with `--no-cache`,
gave identical image IDs for all three after two fixes. The runtimes now
install third-party wheels from uv.lock with `--require-hashes` (they
resolved fresh each build before), the workspace wheels pinned to the
digests just built, and drop `uv_cache.json`/`direct_url.json` and their
RECORD lines.
uv.lock now resolves for Python 3.10 (root `requires-python = ">=3.10"`),
which forks websockets (16.1.1 below 3.11) and numpy.

Images are linux/amd64 only, like the reference.

## Kubernetes chart

`deploy/helm/lazycloud`. Helm rather than kustomize. Migrations need an
ordered pre-install/pre-upgrade hook that Helm and Argo CD both run before
any pod changes, and per-deployment values are the reference's model. Argo
already renders the reference chart through Helm.

- Server Deployment (2 replicas, surge 1, unavailable 0) with Services
  `server` (HTTP, port 80) and `server-hosts` (gRPC 8081). The schema holds
  `server.hostService.type` to ClusterIP until the agent speaks TLS, and
  check.sh proves a LoadBalancer value fails to render. Scheduler
  Deployment (2 replicas, no surge). Both spread across nodes and zones, have
  PodDisruptionBudgets with maxUnavailable 1, run nonroot with a read-only
  root filesystem and no capabilities, and set requests with memory limits
  and no CPU limit.
- Migrations: Job `migrate` running `server migrate` as a
  `pre-install,pre-upgrade` hook. A failure stops the release before pods
  change. Argo maps it to PreSync.
- Configuration: `config` (shared) and `<component>.config` become plain
  `LAZYCLOUD_*` values. Each component lists the Secret keys it reads, so a
  process gets only its own credentials. Rendering fails when config sets a
  secret key or a chart-owned setting. The master key is mounted from the
  Secret as a file at mode 0440 for the pod's fsGroup.
- Secrets: one existing Secret, or `externalSecrets.enabled` projects it from
  Secrets Manager as hooks weighted before the migration Job (secrets-reader
  service account with an IRSA role, SecretStore, ExternalSecret with
  `creationPolicy: Orphan` so recreating the hook never deletes the Secret).
- NetworkPolicy (on by default): ingress to every release pod is denied
  except `networkPolicy.ingressFrom` peers to the API port, `hostCIDRs` to
  the host port and `nodeCIDRs` to the health ports. Rendering fails without
  `nodeCIDRs`, because the kubelet could not probe the pods.
- Isolation: the chart lives at `deploy/helm`, not `deploy/chart`, which the
  production Argo Application reads from the `prod` branch. There is no Argo
  Application for it and no environment values file; `deploy/helm/
  example-values.yaml` holds placeholders only.

`deploy/check.sh` runs helm 4.3.0 lint `--strict` and template (example and
defaults), kubeconform 0.8.0 `-strict` against Kubernetes 1.36.0 and the
External Secrets CRD schemas (both schema sources pinned to commits), and
terraform 1.16.4 `fmt -check`, `init -backend=false -lockfile=readonly` and
`validate`, all from pinned images. Result: 27 resources valid; Terraform
valid.

## Terraform

`deploy/terraform/images` adds only what the reference roots lack, and
creates it in an order that never leaves the role trusting an ungated job:

1. The GitHub `images` environment with required reviewers
   (`release_reviewer_user_ids`, at least one) and a deployment rule
   admitting `platform-v*` tags only.
2. A tag ruleset: only repository administrators create, move or delete
   `platform-v*` tags.
3. The repository OIDC subject template `repo, context, job_workflow_ref,
   ref`, so a subject names the workflow file and the tag.
4. ECR repositories `lazycloud/release/{server,scheduler,agent}` (immutable
   tags, scan on push, untagged expiry after 14 days), apart from the
   reference's `lazycloud/<image>` repositories.
5. The `lazycloud-image-release` role, which depends on 1 to 3. It trusts
   only `release.yml` at a `platform-v*` tag in the `images` environment and
   may push only to the three repositories.
6. The environment variables the workflow reads (role, registry, region).

The subject template applies to every workflow in the repository, so the
reference deploy and release roles, which match `repo:...:environment:<env>`,
would stop matching and Ship to production would fail. The root refuses to
plan until `accept_repository_subject_change = true`, which an operator sets
only after updating those two trusts. Release tags use `platform-v*`
because the reference Ship workflow cuts `v*`.

Providers are pinned (aws 6.67.0, github 6.13.0) with a committed lock.

Reused from the reference rather than duplicated:

- platform-core: the EKS cluster, its OIDC provider and
  `lazycloud/workload-images`.
- platform-deployment, instantiated for a new non-production deployment name:
  the database, buckets, Secrets Manager documents, the control-plane role
  and its Pod Identity associations (pass `control_plane_service_accounts =
  {server = "lazycloud-server", scheduler = "lazycloud-scheduler"}`), the
  secrets-reader role and the account GitHub OIDC provider.
- Cloudflare and the public ingress connector for the dashboard and API
  hostname.

## CI

- `.github/workflows/deploy.yml`: on PRs touching deploy, cmd, Go or Python
  sources, runs `deploy/check.sh` and builds all images without pushing.
- `.github/workflows/release.yml`: on a `platform-v*` tag, calls go.yml,
  python.yml, web.yml and deploy.yml (each gained `workflow_call`), then,
  after a reviewer approves the `images` environment, assumes the release
  role over OIDC, logs in to ECR, pushes with bake as version `v...` and
  writes the digests to the job summary. It deploys nothing
  and says so; there is no Ship equivalent, and the checks are owner tests,
  not platform acceptance. Third-party actions are pinned by commit.
- actionlint 1.7.12 reports only the repository's existing `ubuntu-26.04`
  runner label as unknown.

## Gaps before a deployment can serve users

- Host connection: agents speak plaintext gRPC to the server, so
  `server-hosts` must stay cluster-internal. Exposing it needs agent TLS
  (compute packet) and an NLB.
- Object store credentials: the server and scheduler require a static key
  pair (`LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY`). On EKS
  they should use the Pod Identity role (storage packet).
- Database: LISTEN/NOTIFY and session advisory locks need a direct
  PostgreSQL URL, not the reference's PgBouncer transaction pool. pgxpool
  sizes itself from the node's CPU count; bound it with `pool_max_conns` in
  `LAZYCLOUD_DATABASE_URL`.
- The secrets master key is a new property in the platform document (32
  random bytes, base64, `decodingStrategy: Base64`).
- Rotated secret values reach pods only on restart.
- Not run: any cluster install, Argo sync, External Secrets projection,
  Terraform plan or apply, ECR push, or the release workflow itself.

## Proposed shared changes

Separate `Propose:` commits for the integrator:

- `Remove Storage.RunSweeper now that the scheduler loop runs Sweep`.
- `Lock the Python workspace for 3.10 so managed runtimes build from uv.lock`
  (root pyproject.toml and uv.lock).
- `Accept group-readable master key files, as Kubernetes mounts secrets` and
  `Allow a group-readable master key only when the process is in the file's
  group` (internal/secrets: group read passes only for the process's own or
  a supplementary group; group write or any other access fails).
- `Delete workspace objects in bounded batches per deletion pass`
  (internal/storage `DeleteWorkspaceObjects` now returns whether the prefix
  is empty).
- go.yml, python.yml and web.yml gained `workflow_call` so the release
  workflow can require them.

## Deploying (needs user authorization)

Each step changes AWS, GitHub or a cluster, and none has run.

1. Update the reference deploy and release role trusts to the new subject
   format (`repo:<repo>:environment:<env>:job_workflow_ref:...:ref:...`).
2. Apply `deploy/terraform/images` with the `default` profile after checking
   the STS identity, using the operator backend file, a repository-admin
   `GITHUB_TOKEN`, the reviewer ids and `accept_repository_subject_change =
   true`. It creates the environment, rules and variables before the role.
3. Confirm the next reference Ship still assumes its deploy role, then push
   a `platform-v*` tag and approve the release job.
4. Instantiate the reference platform-deployment for a non-production name
   with the service account map above, and add the master key and direct
   database URL to its secret documents.
5. Install the chart into that namespace with a private values file:
   `helm upgrade --install lazycloud deploy/helm/lazycloud -n <namespace> -f
   <values>`, or an Argo Application pointing at `deploy/helm/lazycloud`.
6. Bootstrap the first administrator:
   `kubectl exec deploy/server -- /usr/local/bin/server admin create-user
   --email <email> --admin`. The first GitHub sign-in with that verified
   email links it.
7. Cutover of production is a separate decision. It needs the gaps above
   closed and platform acceptance, and the reference scheduler stopped
   before the new one runs.
