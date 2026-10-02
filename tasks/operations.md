# Operations packet

Parity sections: "Operations and administration" and "Local development, CI
and deployment" in update.md, plus the dashboard's admin settings Users API.
Nothing here needs schema.

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
- The dashboard lists accounts through `/v1/billing/accounts`, as the
  reference did. Nothing read the reference's user list after its admin CLI
  went, so there is no `GET /v1/users`.

Tests: TestAccountAdministrationNeedsAnAdministratorAccount,
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

## Python package

`lazycloud-client` is the only published Python package. The former
`lazycloud-shared` lives inside it: generated API models and their bases in
`lazycloud.contracts`, the other contracts and helpers in `lazycloud._shared`.
The runner depends on `lazycloud-client`. Neither home imports the rest of the
SDK, and `lazycloud/__init__` no longer loads the API models, so the runner's
imports stay as cheap as before.

Import cost in the 3.12 managed runtime (bytecode precompiled, median of 21
`-X importtime` runs, summed top-level cumulative):

| Statement | Separate package | Folded |
| --- | --- | --- |
| `import runner.protocol` | 92.9 ms | 91.7 ms |
| `import runner.protocol, lazycloud` | 158.3 ms | 94.8 ms |
| `import runner.protocol, lazycloud.abstractions.app` | 224.1 ms | 222.7 ms |

A user module that declares an app still loads the API models through the
SDK abstractions, so container starts cost the same.

## Deployment

The deploy packet (tasks/deploy.md) took the chart, Terraform and release
workflows from here to production: one deployment root with Cloudflare and
Stripe folded in, Neki for PostgreSQL, the host port behind a TLS NLB (the
agent speaks TLS), the dashboard and cloudflared in the chart, Ship in place
of the platform-v* release, and the bring-up runbook.

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
