# Deployment

Deploy builds all control-plane images for the selected commit. Helm records the
network image's executable manifest digest in `image.networkDigest`; the other
images use immutable commit tags. An unchanged network image keeps the WireGuard
Deployment unchanged during an API release. Changes to its source, dependencies,
or system packages select a new digest automatically. Ship and Promote use the
same selection, with no separate network release input or Terraform apply.

CI validates infrastructure, release pins, and database pool budgets before the
build. After the image exists, it resolves the single linux/amd64 runtime manifest
and validates the completed Helm values before recording the deployment. Missing
or ambiguous artifacts fail the deployment rather than retaining stale code.

Root `compose.yaml` is the canonical local stack. This file is the operator
runbook for it; the subdirectory READMEs cover individual services and assets.

## Connected-AWS acceptance environment

### Profiles

`default-test` is the only AWS profile for acceptance work: a role profile
chaining through `default-test-source` into the `lazycloud-default-test-operator`
role.

The `default` profile is root bootstrap authority. Never use it for tests,
Compose, or stack automation; its only accepted use is one-time provisioning
explicitly directed by the owner.

The operator role deliberately cannot create or delete CloudFormation stacks
directly. Customer `compute-connection-*-g*` stacks require the execution role
recorded in the protected `.env` as
`LAZYCLOUD_E2E_AWS_CUSTOMER_STACK_EXECUTION_ROLE_ARN`, which is also the
`CustomerStackExecutionRoleArn` output of the `lazycloud-default-test-operator`
stack. `connected-aws/customer_stack.py` accepts it as `--execution-role-arn`.

The acceptance host may run AWS CLI v1: never pass v2-only flags such as
`--no-cli-pager`. Set `AWS_PAGER=""` in the subprocess environment instead.

Publishing is the exception, and it needs a *newer* CLI than reading does.
`aws-release-assets/release.py` and `ami/catalog.py` use conditional S3 writes for
immutable objects, so a retry after a dropped connection cannot overwrite bytes
that already landed. Conditional writes reached the CLI well after v2.15, and an
older one fails with `Unknown options: --if-none-match` before uploading. Both
commands take `--aws-cli`, so point them at a current binary rather than upgrading
the host.

### Activation

`docker compose up` is the only activation path. The control plane and scheduler
mount `LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR` (default `~/.lazycloud/compose-aws`) at
`/run/lazycloud/aws` and read the role chain from it, so a connected stack
differs from a local one by that variable alone.

That directory holds only the test source credentials and the role-chain profiles
ending in the control principal, never the root `default` keys. The SDK refreshes the
chain, so no fixed session expiry exists. A stack whose credentials do not
resolve refuses to start rather than reporting healthy and failing every
connection later.

### Recreating the control plane

```sh
docker compose up -d --build control-plane wireguard-platform
docker compose ps control-plane wireguard-platform tunnel-gateway
```

`wireguard-platform` shares the control plane's network namespace. Recreate it
with the control plane so it does not remain attached to a replaced namespace.
The separate `tunnel-gateway` service keeps its network namespace and continues
serving enrolled agents.

Workers send runtime callbacks to `100.96.0.1:9000`, the private gateway address.
The gateway forwards them to `control-plane:9000`, whose Kubernetes Service
selects a ready API replica. Helm supplies that Service host and port; Compose
supplies its control-plane service name. The gateway refreshes Service DNS rather
than retaining a replaced API container's address.

Agents and platform sidecars probe the gateway through WireGuard. When it is
unreachable, they refresh the configured endpoint's DNS without removing the
interface or replacing peer keys. This recovery is provider-neutral. Existing
hosts need an updated host agent binary to receive it; shipping a new worker
container image alone does not update that binary. Selecting a new host release
updates future installations, not running agents. Upgrade existing hosts one at
a time through the installer's `--install-only` mode and the agent restart
command, preserving their service arguments and enrollment state. Cordon each
worker and finish its active work before restarting its host agent, then verify
private connectivity before uncordoning it.

The stack is usable when all three services are healthy. The platform sidecar's
readiness proves it can reach the active gateway's health listener through
WireGuard.

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

Compose publishes gateway UDP port 51820. Its bundled agent uses the in-network
default `tunnel-gateway:51820`. Set
`LAZYCLOUD_WIREGUARD_PUBLIC_ENDPOINT=<host>:51820` when agents reach the host by
another address. The host may be a DNS name from any provider, but it must reach
the gateway over UDP. A Cloudflare HTTP tunnel cannot carry WireGuard traffic.

`wireguard-key-bootstrap` creates one gateway keypair and one platform keypair
in the `wireguard-keys` named volume. The volume keeps those identities stable
across container recreation. Agents keep their own private keys in their state
directories; Postgres stores only their public keys and assigned addresses.

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
