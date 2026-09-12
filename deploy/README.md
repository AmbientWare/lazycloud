# Deployment

Ship publishes the platform images from Docker Bake and records their executable
digests in one complete release manifest. An unchanged connection gateway image keeps its
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
docker compose ps control-plane connection-gateway connection-gateway-1 connection-ingress
```

The API and connection gateways run independently. Recreate the API without
recreating the gateway services.

Workers and user containers send callbacks to their local agent connector. Its
authenticated outbound tunnel carries them to a gateway, which opens the API
Service at `control-plane:9000`. API replica addresses never become agent state.

Check authenticated session ownership and logs at the agent and gateway when a
machine is unreachable. Gateway process health alone does not prove that route.

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

The stack is usable after an authenticated agent session can run a real function.
API and gateway health prove their processes and dependencies, not workload routes.

### Enrolled local capacity

The `agent` service joins a machine owned by the `customer` account. Its
`tenant-customer` workspace selects the `self-hosted` pool. The agent launches
the built worker image in its own network namespace and registers authenticated
routes through the outbound tunnel.

The agent's Compose entrypoint also forwards loopback port 5000 to the local
workload registry. The worker shares that namespace. This registry listener is
bound only to 127.0.0.1 and exits with the agent; it carries no control-plane RPC.

The `container-worker` Compose entry belongs to the `build` profile and never
runs as a standalone service. `deploy.release` builds that image before publishing
its immutable local image ID to the agent. Workspaces without a configured
enrolled machine have no local execution capacity.

The customer's machine uses its own state directory, bridge and subnet. Keep
those distinct when adding another local agent, and allow disk space for its
image cache and build scratch.

### Resetting local state

Resetting means Postgres, Redis, and the agent together. Redis is keyed by durable
IDs, so a recreated database leaves the scheduler refusing every reconcile against
capacity owners the new database does not know.

The agent's state directory is a host bind mount whose enrollment and worker slots
outlive both. Clear `slots/`, `agent-state.json`, `active-worker-slots.json`, and
`runtime-ready.json`. Leaving it while resetting the database points that
enrollment at a database with no record of its machine. Leave `images/` alone
unless the image cache is the thing being tested.

### Agent tunnel endpoint and credentials

Compose runs two connection gateways behind HAProxy TCP passthrough on host port
443. Agents reach `tunnels.lazycloud.test:443`; worker and container requests cross the
agent's local connector. `tunnel-issuer-bootstrap` initializes the CA once in its
own volume. API pods alone hold its key; gateway and agent keys remain separate.
See [connection gateway deployment](connection-gateway.md) for credentials, local
startup, and production DNS. Recreating the API preserves gateway connections.


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
