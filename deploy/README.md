# Deployment

Ship publishes the platform images from Docker Bake and records their executable
digests in one complete release manifest. An unchanged network image keeps the WireGuard
Deployment unchanged during an API release. Changes to its source, dependencies,
or system packages select a new digest automatically. Ship and Promote use the
same selection, with no separate network release input or Terraform apply.

Deploy renders Helm from that manifest and the environment's infrastructure descriptor.
Argo rolls out the chart, then publishes the active-release ConfigMap after its
health checks pass. API and scheduler use its mounted file to gate worker admission.
Use `uv run --group workspace python -m deploy.release` to build and activate local
Compose images through the same runtime admission contract.

Root `compose.yaml` is the canonical local stack. This file is the operator
runbook for it; the subdirectory READMEs cover individual services and assets.

For cloud capacity, start with [Provider provisioning](PROVIDERS.md). It names
the shared AWS/Hetzner deployment flow, ownership boundaries, required inputs,
and the contract another provider must implement.

## Local environment

Run `bash deploy/setup-local-env.sh` from a checkout. It creates `.env` from
`.env.example` when needed and installs the repository's post-checkout hook.
The main checkout owns the private file; worktrees link to it. Existing `.env`
files and unrelated Git hooks are preserved. Setup generates missing local
infrastructure credentials and keeps existing values. Fill the GitHub App and
Stripe fields with development credentials.

Start the local platform with:

```sh
uv run --frozen --group workspace python -m deploy.release
```

Inspect `docker compose ps`
and the service logs to confirm startup. The API serves the built dashboard at
`http://lazycloud.localhost:8000`. For frontend edits, run `bun run dev` from
`apps/web`; its default API target is `http://127.0.0.1:8000`.

Compose initializes Garage 2.3, publishes the reviewed billing rates, and
creates the local administrator. Garage's S3 endpoint is
`http://object-store.localhost:3900`; the Admin API binds only to loopback on
port 3903. Application and workspace buckets use the configured platform key.
The worker receives a separate bucket key with a fifteen-minute expiration.
It refreshes through the same credential interface used by AWS STS. Garage enforces
expiration; the issuer removes expired workspace keys on subsequent issuance.
The S3 client, volume mount, artifact and billing implementations are shared.

Keep the object-store endpoint reachable from the host, API and worker. Compose
resolves `object-store.localhost` to Garage inside containers; the host resolves
it to loopback. A browser on another machine also needs access to that endpoint
for signed storage transfers.

Use the public SDK against this deployment by exporting `LAZYCLOUD_ENDPOINT` and
the local `LAZYCLOUD_TOKEN`. `examples/artifacts/app.py` writes through a volume
and saves an artifact with seven-day retention. A token-only administrator can
provision its Stripe test subscription through `/api/v1/billing/card-session`
before running workloads, just as an account does when adding a card. An existing
Stripe test catalog must contain the current plans; `lazycloud-admin billing
publish-catalog` reports missing entries before publishing with `--confirm`.

An explicit `VITE_API_TARGET` override can point a web preview at a remote API.
That override does not change local backend credentials or configure GitHub's
callback. Browser sign-in must return to an origin that serves the callback and
retains the sign-in cookie.

Cloud capacity and public ingress are opt-in. Keep provider acceptance settings
out of the default startup configuration unless that is the run being performed.

## Local database connections

`docker compose up` builds PgBouncer and starts it after PostgreSQL is healthy.
Applications connect to `pgbouncer:6432`; migrations and session-scoped advisory
locks use `LAZYCLOUD_DATABASE_DIRECT_URL` at `postgres:5432`. The host-shell URLs
in `.env.example` use the corresponding localhost ports. Change
`LAZYCLOUD_COMPOSE_PGBOUNCER_PORT` and the host application URL together if 6432
is occupied.

The pooler runs in transaction mode with 200 client slots and at most 20 backend
connections for the local database user. It tracks 200 protocol-level prepared
statements per backend, so psycopg's automatic preparation stays enabled.
The image pins the Alpine digest and verifies the upstream PgBouncer 1.25.2
source checksum. See the [PgBouncer configuration](https://www.pgbouncer.org/config.html)
and [release notes](https://www.pgbouncer.org/changelog.html).

PgBouncer uses the same `POSTGRES_PASSWORD` as PostgreSQL. Its entrypoint writes
the authentication file into a private tmpfs, not the image or a persistent
volume. Its health check authenticates and executes `SELECT 1` through the pool.
After changing an existing database role's password, update both host URLs and
`POSTGRES_PASSWORD`, then recreate PgBouncer and the application services.
Do not reset the PostgreSQL volume to rotate a password.

## Connected-AWS acceptance environment

### Profiles

`default-test` is the AWS profile for customer BYO-cloud acceptance: a role profile
chaining through `default-test-source` into the `lazycloud-default-test-operator`
role.

Use `default` for owner-authorized deployments of our platform. The selected
account currently exposes root authority through this profile, so confirm the
account and exact resources before mutation. Do not copy its credentials into
workloads or CI. GitHub deployments use their configured OIDC role.
The test role is not a prerequisite for platform deployment.

The operator role deliberately cannot create or delete CloudFormation stacks
directly. Customer `compute-connection-*-g*` stacks require the execution role
recorded in the protected `.env` as
`LAZYCLOUD_E2E_AWS_CUSTOMER_STACK_EXECUTION_ROLE_ARN`, which is also the
`CustomerStackExecutionRoleArn` output of the `lazycloud-default-test-operator`
stack. `connected-aws/customer_stack.py` accepts it as `--execution-role-arn`.

The acceptance host may run AWS CLI v1: never pass v2-only flags such as
`--no-cli-pager`. Set `AWS_PAGER=""` in the subprocess environment instead.

Release binaries, templates, catalogs, and deployment descriptors use S3.
`aws-release-assets/release.py` and `ami/catalog.py` publish through `deploy.object_storage`,
which uses the caller's AWS identity, conditional writes, and a
SHA-256 check of the downloaded object. CloudFront serves public release URLs.
AWS CLI remains responsible for ECR,
AMI inspection, and customer CloudFormation operations.

Platform S3 access logs use a private bucket and an SQS queue from the infrastructure
descriptor. `LAZYCLOUD_AWS_STORAGE_ACCESS_BUCKET` and
`LAZYCLOUD_AWS_STORAGE_ACCESS_QUEUE_URL` use the scheduler's existing AWS identity.
The issuer enables logging before granting workspace access. Customer buckets keep
their own storage configuration.

The scheduler retains deduplicated request observations in PostgreSQL. It acknowledges
each queue message only after its log objects are recorded. Failed messages retry,
then enter the dead-letter queue after ten receives. Inspect scheduler errors and
queue age, fix the cause, then redrive that queue. Messages expire after 14 days and
raw log objects after 30 days. Raw logs contain signed URLs and must stay private.
Logs can arrive late or omit requests; region evidence does not prove a paid transfer.
Observed requests and response bytes remain unbilled and appear in the economics report.

Customer authorization discovers all enabled standard availability zones through
the customer's AWS credentials and supplies them to the connection template,
which supports two to six zones.
The customer profile needs `ec2:DescribeAvailabilityZones`. Default VPC subnets
are not required; authorization stops before creating a stack if fewer than two
zones are available. The public CLI and `connected-aws/customer_stack.py` use the
same SDK transport for discovery and stack creation.

### Activation

The control plane and scheduler
mount `LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR` (default `~/.lazycloud/compose-aws`) at
`/run/lazycloud/aws` and read the role chain from it. Managed capacity also needs a
complete published release. The local source release command requires an unpinned
Compose environment.

That directory holds only the test source credentials and the role-chain profiles
ending in the control principal, never the root `default` keys. The SDK refreshes the
chain, so no fixed session expiry exists. A stack whose credentials do not
resolve refuses to start rather than reporting healthy and failing every
connection later.

### Recreating the control plane

```sh
uv run --frozen --group workspace python -m deploy.release
docker compose ps control-plane wireguard-platform tunnel-gateway tunnel-gateway-1
```

`wireguard-platform` shares the control plane's network namespace. Recreate it
with the control plane so it does not remain attached to a replaced namespace.
The separate gateway services keep their network namespaces and continue serving
enrolled agents.

Workers send runtime callbacks to `100.96.0.1:9000`, the private gateway address.
The gateway forwards them to `control-plane:9000`, whose Kubernetes Service
selects a ready API replica. Helm supplies that Service host and port; Compose
supplies its control-plane service name. The gateway refreshes Service DNS rather
than retaining a replaced API container's address.

Agents and platform sidecars probe the gateway through WireGuard. When it is
unreachable, they refresh the configured endpoint's DNS without removing the
interface or replacing peer keys. This recovery is provider-neutral. Existing
hosts need an updated host agent binary to receive it; shipping a new worker
container image alone does not update that binary.

For managed AWS pools, selecting a new host release changes the launch template.
The pool controller adds a replacement, checks that it enrolled, drains the
superseded host, and retires it after its work finishes. It then removes the
temporary capacity. Let that controller complete; do not also run installer
updates on hosts it is replacing.

Self-hosted machines do not use that replacement path. Upgrade them one at a
time through the installer's `--install-only` mode and the agent restart command,
preserving their service arguments and enrollment state. Cordon each worker and
finish its active work before restarting its host agent, then verify private
connectivity before uncordoning it.

The stack is usable when the API, platform sidecar and both gateways are healthy.
The sidecar proves at least one encrypted gateway path. Each gateway requires a
platform handshake before becoming ready, so the sidecar starts after gateway
process creation without depending on their readiness.

### Shared fleet and one customer machine

The stack runs the two kinds of capacity a workload can land on, so the choice
between them is exercised by the ordinary local stack rather than assembled by hand.

| | `container-worker` | `agent` |
| --- | --- | --- |
| Is | the shared LazyCloud fleet | one customer's own machine |
| Runs as | a platform service with a `worker` service token | an agent joining with a join token |
| Owned by | nobody; shared capacity has no account | the `customer` account |
| Pool | `lazycloud` | `self-hosted` |
| Private | no — serves every account | yes — serves only its owner's workspaces |
| Reached by | any workspace that names no pool | `tenant-customer`, whose `default_pool` names it |
| Container bridge | `rt_br2`, `192.168.32.0/20` | `rt_br0`, `192.168.0.0/20` |

`default` is left with no compute policy on purpose: a workspace that names no pool
gets one written for it pointing at `lazycloud`, which is what a real new signup
gets, and it lands on the shared fleet. The customer's workspace names
`self-hosted`, so its work goes to the machine its account connected. Placement
compares accounts rather than workspaces, so the shared worker takes anyone's work
while the joined machine refuses everyone but its owner.

Three things must differ between the two workers, and none of them fails visibly
when it does not: the machine fingerprint, which decides the machine id, so a shared
one makes them evict each other's credentials; the bridge name and subnet, because
each allocator issues addresses inside its own control-plane scope and a shared
bridge is two allocators handing out one address with no lock between them; and the
pool, because a workspace whose `default_pool` names the other's pool has its work
placed where that worker will refuse it.

The customer account is created as a platform administrator only because creating a
workspace is an administrator action. What the stack is exercising is that a second
*account* owns the machine, not what that account may do elsewhere.

Both state directories want daemon-local storage with room for their own image cache
and build scratch — nothing is shared between them.

### Resetting local state

Resetting means Postgres, Redis, and the agent together. Redis is keyed by durable
IDs, so a recreated database leaves the scheduler refusing every reconcile against
capacity owners the new database does not know.

The agent's state directory is a host bind mount whose enrollment and worker slots
outlive both. Clear `slots/`, `agent-state.json`, `active-worker-slots.json`, and
`runtime-ready.json`. Leaving it while resetting the database points that
enrollment at a database with no record of its machine. Leave `images/` alone
unless the image cache is the thing being tested.

The shared fleet needs nothing cleared: its unit, token, and worker record are all
derived on start, and its volumes hold only caches.

### WireGuard endpoint and keys

Compose runs two gateways and publishes host UDP ports 51820 and 51821. The
agent and its worker share the agent container's network namespace and reach
the private runtime through WireGuard. Its bundled agent reaches
`tunnel-gateway:51820` and `tunnel-gateway-1:51820`, so local function traffic
exercises the tunnel and forwarding rules. For agents outside Compose, set
`LAZYCLOUD_COMPOSE_WIREGUARD_GATEWAY_0_ENDPOINT` and
`LAZYCLOUD_COMPOSE_WIREGUARD_GATEWAY_1_ENDPOINT` to the corresponding reachable
`<host>:<port>` addresses. A Cloudflare HTTP tunnel cannot carry WireGuard traffic.

`wireguard-key-bootstrap` creates two gateway keypairs and one platform keypair
in the `wireguard-keys` named volume. Existing gateway zero keys stay in `server/`;
gateway one uses `gateway-1/`. The volume keeps those identities stable across
container recreation. Agents keep private keys in their own state directories.
Postgres owns gateway identities, endpoints, public peer keys and assigned addresses.
Redis owns gateway leases and expiring peer path presence. Platform traffic
selects only gateways with presence for the destination's current generation.

The Compose agent owns an isolated network namespace. Its workers join that
namespace through Docker's `container:` network mode, keeping the agent's
tunnels and container forwarding together without changing workstation routes.

The platform sidecar reads both gateways from PostgreSQL. The `control-plane`
service creates its shared network namespace with `src_valid_mark=1`; Docker
does not allow the sidecar to set network sysctls on another container's namespace.
The sidecar mounts only the writable `platform-0` subdirectory of the key volume
so its route journal survives a process restart without exposing gateway keys.
Each gateway has 150 seconds to stop, including its bounded connection drain.

Deleting the key volume changes the gateway and platform identities. During a
complete local reset, delete it together with Postgres and the agent state so
the bootstrap can create a coherent network. Never apply that reset to an
external deployment.

### Public ingress and DNS

`public-ingress` is a locally-managed tunnel: it carries `config_src: local`, so
Cloudflare pushes no configuration and `public-ingress/cloudflared.yml` is the
only source of what is exposed. Dashboard public hostnames do not apply and must
not be added — they read as live routing while changing nothing.

A connector with ready connections and a hostname still returning 1033 is a DNS
problem, not a route problem: the hostname's record is not a proxied CNAME to
this tunnel. `curl` the connector's `/ready` on `127.0.0.1:20241` from inside the
namespace to separate connector health from edge routing.

Cloudflare DNS hides what it is doing at a zone apex. A proxied CNAME to
`<tunnel>.cfargotunnel.com` is flattened to Cloudflare anycast A records, so `dig`
cannot distinguish it from an unrelated proxied A record — read the zone through
the API before concluding anything about apex records. Auto-created tunnel DNS
silently declines to overwrite an existing record, and a record pinned as a
Cloudflare for SaaS fallback origin (SSL/TLS → Custom Hostnames) cannot be
deleted at all until that designation is removed.

`lazycloud.dev` carries live Google Workspace mail: five `MX` records, an SPF
`TXT`, and a site-verification `TXT`. They coexist with the apex CNAME only
because of CNAME flattening. Never clear the zone; delete records by id.
