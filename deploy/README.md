# Deployment

Root `compose.yaml` is the canonical local stack. This file is the operator
runbook for it; the subdirectory READMEs cover individual sidecars and assets.

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

`tailnet-gateway` and `public-ingress` both run in the control plane's network
namespace, so recreating `control-plane` destroys them and Compose does not bring
them back. The stack then reports every service healthy while the control plane
is absent from the tailnet and off the public origin; remote nodes fail to
resolve it as a peer minutes later.

Follow any `control-plane` recreate with:

```sh
docker compose up -d --force-recreate tailnet-gateway public-ingress
```

Plain `up -d` is not enough: the sidecar can stay attached to the namespace of a
control plane that no longer exists, and it stays healthy there.

`tailscale status` reporting `Online: True` does not mean the control plane is
reachable. It describes the sidecar's own session, which is healthy whether or
not anything is listening behind it. Check from inside the shared namespace
instead:

```sh
docker compose exec tailnet-gateway wget -qO- http://127.0.0.1:9000/healthz
```

A refusal there means the sidecar and the control plane are in different
namespaces, whatever the tailnet says.

### Resetting local state

Resetting means Postgres, Redis, and the agent together. Redis is keyed by
durable IDs, so a recreated database leaves the scheduler refusing every
reconcile with `capacity owner … has multiple agent pool configs`.

The agent's `/var/lib/lazycloud/agent` is a host bind mount whose enrollment and
worker slots outlive both. Clear `slots/`, `agent-state.json`,
`active-worker-slots.json`, and `runtime-ready.json`. Leave `images/` alone
unless the image cache is the thing being tested.

### Naming the control plane

Read the control plane's tailnet name from the running sidecar rather than
assuming it. A device that lost its name to a collision keeps the `-1` suffix,
and `LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL` must match what the sidecar actually
holds. Do not delete a tailnet device to reclaim a nicer name: it invalidates the
sidecar's identity and takes the control plane off the tailnet.

Give `LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL` the MagicDNS name, never the tailnet
IP. The address changes when the sidecar re-registers, and a stale one does not
fail at startup — the stack reports healthy and workers cannot reach the control
plane, which surfaces much later as nodes that never report. Confirm it against
`tailscale ip -4` in the sidecar after any change to that service.

Every deployment value naming the control plane has to carry the sidecar's real
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
