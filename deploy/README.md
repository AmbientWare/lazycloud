# Deployment

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
`aws-release-assets/release.py` and `ami/bake.py` write every release object with
`s3api put-object --if-none-match '*'`, so that a retry after a dropped
connection cannot overwrite bytes that already landed. Conditional writes reached
the CLI well after v2.15, and an older one fails the publish with
`Unknown options: --if-none-match` before it uploads anything. Both commands take
`--aws-cli`, so point it at a current binary rather than upgrading the host.

### Activation

`docker compose up` is the only activation path. The control plane and scheduler
mount `LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR` (default `~/.lazycloud/compose-aws`) at
`/run/lazycloud/aws` and read the role chain from it, so a connected stack
differs from a local one by that variable alone.

That directory holds only the test source credentials and the role-chain profiles
ending in `compose-control`, never the root `default` keys. The SDK refreshes the
chain, so no fixed session expiry exists. A stack whose credentials do not
resolve refuses to start rather than reporting healthy and failing every
connection later.

### Recreating the control plane

```sh
docker compose up -d --build control-plane
```

That is the whole procedure. The control plane runs its own `tailscaled`, so
nothing else has to be recreated alongside it and no service borrows its network
namespace.

It rejoins the tailnet under the device identity persisted in the
`control-plane-tailnet-state` volume, minting a fresh tagged key from its OAuth
client only when that identity is missing or expired. Its healthcheck resolves
the host it advertises to workers, so a control plane that came up unable to
reach the tailnet reports unhealthy rather than serving nothing quietly.

### Two tenants

The stack runs two workspaces so multi-tenant behaviour is exercised by the
ordinary local stack rather than assembled by hand. Nothing coordinates the two
agents; four values must differ, and none of them fails visibly when it does not:

| | agent | agent-2 |
| --- | --- | --- |
| Workspace | `default` | `tenant-b` |
| Pool | `lazycloud` | `lazycloud-b` |
| Fingerprint | `compose-agent` | `compose-agent-2` |
| State directory | `/var/lib/lazycloud/agent` | `/var/lib/lazycloud/agent-2` |
| Container bridge | `rt_br0`, `192.168.0.0/20` | `rt_br1`, `192.168.16.0/20` |

The fingerprint decides the machine id, so two agents sharing one derive the same
machine and evict each other's credentials. The bridge decides which addresses
their workers hand out, and each agent allocates inside its own control-plane
scope, so a shared bridge is two allocators issuing the same address with no lock
between them; a bridge already holding an address outside its configured subnet is
refused rather than taken over. Each workspace's pool must match its own agent's,
because a workspace whose `default_pool` names the other agent's pool has its work
placed where the worker will refuse it.

Both state directories want daemon-local storage with room for their own image
cache and build scratch — nothing is shared between them.

### Resetting local state

Resetting means Postgres, Redis, and **both** agents together. Redis is keyed by
durable IDs, so a recreated database leaves the scheduler refusing every
reconcile against capacity owners the new database does not know.

Each agent's state directory is a host bind mount whose enrollment and worker
slots outlive both. Clear `slots/`, `agent-state.json`,
`active-worker-slots.json`, and `runtime-ready.json` in each. Clearing one and
not the other leaves that tenant's enrollment pointing at a database with no
record of its machine. Leave `images/` alone unless the image cache is the thing
being tested.

### Naming the control plane

Read the control plane's tailnet name from the running control plane rather than
assuming it. A device that lost its name to a collision keeps the `-1` suffix,
and `LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL` must match what the device actually
holds. Do not delete a tailnet device to reclaim a nicer name: it invalidates the
control plane's identity and takes it off the tailnet.

Give `LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL` the MagicDNS name, never the tailnet
IP. The address changes when the device re-registers. The control plane's
healthcheck resolves this host, so a stale one now shows up as an unhealthy
container rather than as nodes that never report — but only the name is checked,
not that it points at this deployment.

Every deployment value naming the control plane has to carry the real
device name, `-1` suffix included — `LAZYCLOUD_AWS_CAPACITY_AGENT_BINARY_URL` as
much as the runtime origin. Each is read on a different path, so fixing one
proves nothing about the rest: an agent-binary URL pointing at the pre-collision
name resolved nowhere and failed the boot at `ensure_agent`, long after the
runtime origin had been corrected. Grep the whole file for the bare name.

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
