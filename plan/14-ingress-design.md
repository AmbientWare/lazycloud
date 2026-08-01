# INFRA-06/07/08 — Public HTTPS ingress: concrete design

Owner decision 1 (`plan/final.md`) fixed the mechanism: Cloudflare Tunnel on the
owner's `lazycloud.dev` zone. This document is the implementable shape. It
supersedes the file lists in `plan/13-production-readiness.md` for INFRA-06,
INFRA-07 and INFRA-08 where they disagree; every claim below was re-verified
against the tree on 2026-07-31.

Precondition state, verified in code — Gate A is satisfied:

- Rate limiter live and registered before routing:
  `apps/api/src/api/fastapi_app.py:219-223`,
  `apps/api/src/api/server/rate_limit.py:30-35` (limits; `/health` fails open
  at `:34`, the provider-node prefix fails closed via the `fail_open=False`
  default at `:27`, decision at `:91-97`). Note the actual middleware order,
  outermost→innermost: `GatewayRequestEventMiddleware` →
  `UnauthenticatedRateLimitMiddleware` → `GeneratedInvokeHostRoutingMiddleware`
  (`fastapi_app.py:215-229`; last added is outermost). A refused 429 is 4xx and
  never reaches the 5xx durable-event writer, which is the invariant that
  matters.
- Client address from exactly one configured source:
  `apps/api/src/api/server/client_address.py:8-30`, settings
  `apps/api/src/api/settings.py:48-67`
  (`LAZYCLOUD_PUBLIC_INGRESS_CLIENT_IP_HEADER`, empty default,
  `compose.yaml:48`).
- No AWS call in the provider-node request path; STS proof verification
  token-fenced to 8 in-flight with a 2 s budget:
  `packages/gateway/src/gateway/provider_enrollment.py:289-302`,
  `packages/providers/aws/src/provider_aws/provider_node_identity.py:29-30`,
  `LAZYCLOUD_PUBLIC_INGRESS_PROVIDER_NODE_PROOF_MAX_INFLIGHT`
  (`compose.yaml:49` — plan/13 named this variable without the
  `PUBLIC_INGRESS` segment; the compose name is canonical).

## 1. Topology

One new Compose service, `public-ingress`, running pinned
`cloudflare/cloudflared` in the **control plane's network namespace** — the
same shape as `tailnet-gateway` (`compose.yaml:546-583`). The sidecar dials
out to Cloudflare's edge; nothing listens on a public port; TLS terminates at
the edge; the deployment holds no certificate.

- **Hostname**: apex `lazycloud.dev` → `http://127.0.0.1:9000` (the control
  plane's in-namespace listener, `compose.yaml:446-455`), plus wildcard
  `*.lazycloud.dev` → the same origin. The wildcard is not optional:
  `GeneratedInvokeHostRoutingMiddleware` derives its base host from
  `LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL` and routes `<stub>.<base>` to deployed
  endpoints/pods (`apps/api/src/api/server/host_routing.py:41-47, 63-71`). The
  base **must be the apex**: Cloudflare Universal SSL covers exactly one
  wildcard level (`*.lazycloud.dev`), so a base of `api.lazycloud.dev` would
  put invoke hosts at `*.api.lazycloud.dev` outside the certificate — an
  Advanced Certificate Manager dependency this design refuses. Invoke
  subdomains are single-label as a consequence; multi-label stub hosts have no
  TLS and no support.
- **Image pinning**: `cloudflare/cloudflared:2026.7.3` (current release,
  verified against the registry 2026-07-31), overridable through
  `LAZYCLOUD_CLOUDFLARED_IMAGE` exactly as the Tailscale image is pinned at
  `compose.yaml:234`.
- **Credentials**: a locally-managed tunnel. `cloudflared tunnel create
  lazycloud-control-plane` mints a credentials JSON (`<TUNNEL_ID>.json`) that
  never enters the repo; the deployment `.env` names its host path in
  `LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE` and Compose mounts it read-only.
  The tunnel UUID itself is an identifier, not a secret, and is committed in
  `deploy/public-ingress/cloudflared.yml`. The `CLOUDFLARE_TUNNEL_TOKEN` in
  `~/.lazycloud-legacy-secrets/secrets-backup/` belongs to the superseded
  architecture and sat unencrypted on disk — it is **re-minted, never
  reused**: create the new tunnel, then delete the old tunnel and rotate the
  old token/API key as part of the INFRA-18 owner rotation
  (`plan/final.md`, Phase 0 INFRA-18 note).
- **Coexistence with `tailnet-gateway`**: both sidecars share the
  control-plane namespace, so **recreating `control-plane` destroys both and
  Compose brings back neither** (`deploy/AGENTS.md:43-48`,
  `plan/00-prior-tailnet-plan.md:108`). Every `control-plane` recreate is
  followed by `docker compose up -d tailnet-gateway public-ingress`. The
  tailnet keeps the data path only (control plane → agent route proxy, workers
  dialing `LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL`, `compose.yaml:24`); the public
  path is the tunnel. `deploy/AGENTS.md` gains the second sidecar in its
  recreate rule in the same change.
- **Service presence**: `public-ingress` sits behind the Compose profile
  `public-ingress`, activated by `COMPOSE_PROFILES=public-ingress` in the
  deployment `.env`. This is deployment topology, not a code path — the
  process serves one route either way — and forgetting it is loud, not silent:
  INFRA-07 (section 5) refuses to start managed capacity against a loopback
  public origin, so a connected stack without its tunnel exits at composition
  instead of failing nodes twenty minutes into a boot.
- **Out of scope, named**: the TCP ingress on port 1995
  (`compose.yaml:38-46, 491`) is a separate TLS-over-TCP surface with its own
  certificate; Cloudflare's HTTP proxy does not carry it and this design does
  not change it.

New files: `deploy/public-ingress/cloudflared.yml`,
`deploy/public-ingress/README.md` (mint + rotation procedure),
`deploy/public-ingress/AGENTS.md` with its `CLAUDE.md` symlink (recording the
namespace-recreate hazard and the credentials rule).

## 2. What is exposed and what is not

The origin (`127.0.0.1:9000`) serves the entire API. The public surface is
everything a public consumer legitimately dials; exactly two prefixes are
internal-only and are refused **at the tunnel**, before the origin sees the
request. Enforcement is the ordered `ingress` rule list in
`cloudflared.yml` — first match wins, path rules are anchored Go regexes —
which keeps the allowlist in-repo and reviewed, not in a dashboard.

Forwarded on `lazycloud.dev`:

| Prefix | Consumer | Evidence |
| --- | --- | --- |
| `/api/v1/` | SDK/CLI/dashboard, bearer-authenticated | routers under `apps/api/src/api/server/routers/` (volumes, taskqueues, functions, images, pods, artifacts, shells, signals, endpoints `/api/v1/{endpoints,asgi}`, resource_api, control_plane, `/api/v1/tokens/all`) |
| `/gateway/` | agents (join/stream/telemetry batch), managed-node enrolment (`/gateway/provider-nodes/*`, unauthenticated, rate-limited fail-closed), SDK gateway RPC (containers/tasks/stubs/deployments/auth objects) | `routers/gateway/*.py`, all `prefix="/gateway"`; agents dial the public origin after BOOT-04/05, direct machines already do (`plan/10-bootstrap-enrolment.md`, BOOT-04) |
| `/auth/device`, `/auth/device/token`, `/auth/authorize` | device auth flow, rate-limited | `routers/system.py:286, 310, 380` |
| `/health` | probes + acceptance, rate-limited fail-open | `system.py:83` |
| `/install/agent` and `/install/agent/...` | install script (managed nodes after BOOT-05, direct machines today) and the versioned/unversioned binary for direct machines | `routers/install.py:23, 43, 75` |
| `/pod/`, `/sandbox/` | public/direct invoke routes | `routers/pods/proxy.py:90-91` |
| `/` (everything else) | dashboard SPA + static assets | `apps/api/src/api/web_static.py:43` mounts the SPA at `/` |

Forwarded on `*.lazycloud.dev`: **all paths** — the host-routing middleware
rewrites them to the stub's handler path internally
(`host_routing.py:84-112`); a path filter here would break user apps.

Refused at the tunnel (edge returns 404, origin never dialed):

| Prefix | Why internal-only | Evidence |
| --- | --- | --- |
| `/metrics` | admin scrape surface; Prometheus reads `http://control-plane:9000/metrics` inside the Compose network, never through the ingress | `system.py:112`; `plan/13-production-readiness.md` INFRA-15 |
| `/worker-repository/` | worker RPC; workers dial the internal origin (`WORKER_REPOSITORY_URL`, `compose.yaml:816`; Docker-gateway rewrite at `:296-303`) or the tailnet runtime origin — never the public one | `routers/worker_repository.py:161-...` |

Everything else that looks like a path is one of the two forwarded families
(an `/api/v1/*` route or the SPA fallback); the route inventory was enumerated
from every `APIRouter` prefix and literal route path to confirm there is no
third internal-only family.

Agent artifact placement, checked rather than assumed: managed nodes fetch the
47.6 MB binary from the **release bucket** (`LAZYCLOUD_AWS_CAPACITY_AGENT_BINARY_URL`,
`compose.yaml:116`; validated in
`packages/compute/src/compute/node_bootstrap.py:99-145`; bucket provisioned by
`deploy/aws-release-assets/cloudformation.yaml`) — owner decision 2. After
BOOT-05 they additionally pull the few-KB `/install/agent` script from the
public origin and pass `--agent-url` back to the bucket, so the bulk transfer
never transits the tunnel. The control-plane binary routes
(`install.py:43-127`) stay forwarded for direct machines; their traffic is
low-rate by construction.

Streaming and body limits, checked per route:

- Cloudflare severs a proxied response after ~100 s without a byte. The
  workspace change stream heartbeats every 15 s
  (`packages/observability/src/observability/workspace_changes.py:25`) and the
  agent event stream emits a snapshot every poll interval (default 5 s,
  `routers/gateway/agents.py:163, 200-210`) — both fine. The task-log follow
  stream emits **nothing while a task is quiet**
  (`routers/resource_api/observability_logs.py:167-186`): an idle
  `/api/v1/logs/stream?...&follow` dies at the edge. **Companion change inside
  INFRA-06**: `_follow_task_logs` yields a `": heartbeat\n\n"` comment frame
  after ≤15 s without an event, matching the change-stream cadence. This is
  precisely what INFRA-06's acceptance bullet ("two SSE frames more than 100 s
  apart") exists to catch.
- No long-lived request body crosses the edge: agents post telemetry batches
  (`apps/agent/src/agent_app/daemon.py:428`), and
  `/gateway/agents/telemetry/stream` has no production consumer outside the
  API and its own test. The 100 MB free-plan request-body cap binds only on
  direct pod file uploads (`routers/pods/files.py`); artifact/object bulk
  transfer uses presigned object-store URLs that bypass the tunnel entirely —
  a connected deployment presigns against the storage endpoint itself
  (`compose.yaml:57-61`).
- WebSocket routes (`.../ws`, `pods/proxy.py`) are supported by Cloudflare
  Tunnel unchanged.

### The client-IP header contract

Cloudflare sets `CF-Connecting-IP` **at the edge from the TCP peer and
overwrites any client-supplied value on every proxied request** — that is the
strip-and-rewrite guarantee `client_address.py` requires. The deployment sets
`LAZYCLOUD_PUBLIC_INGRESS_CLIENT_IP_HEADER=CF-Connecting-IP`; the resolver
takes the last value of that header and ignores the socket peer
(`client_address.py:25-30`).

Two consequences, both named:

1. **Setting the variable is mandatory in the tunnel deployment**, not
   optional hardening: every tunneled request's socket peer is `127.0.0.1`
   (shared namespace), so with the header unset the whole public internet
   shares one rate-limit bucket and the limiter throttles everyone together.
2. The trust boundary is the tunnel: the only unproxied paths to the origin
   are host port 8000 (`compose.yaml:490`) and the Compose network — both
   operator-controlled positions (the local worker and agent services depend
   on the host port, `compose.yaml:296-303, 666-671`, so it stays published).
   A forger there is the operator. The deployment host exposes no inbound
   port; that is the point of the tunnel.

## 3. Exact config

`compose.yaml` — new service, placed beside `tailnet-gateway`:

```yaml
  public-ingress:
    # Public HTTPS ingress: a Cloudflare Tunnel sidecar in the control plane's
    # network namespace. Present only in a deployment that has minted a tunnel
    # (COMPOSE_PROFILES=public-ingress); INFRA-07 makes a connected stack
    # without it refuse to start rather than fail its nodes remotely.
    # Recreating control-plane destroys this sidecar and Compose does not
    # bring it back: follow any recreate with
    #   docker compose up -d tailnet-gateway public-ingress
    profiles: ["public-ingress"]
    image: "${LAZYCLOUD_CLOUDFLARED_IMAGE:-cloudflare/cloudflared:2026.7.3}"
    restart: unless-stopped
    depends_on:
      control-plane:
        condition: service_started
    network_mode: "service:control-plane"
    command:
      [
        "tunnel",
        "--no-autoupdate",
        "--config",
        "/etc/cloudflared/config.yml",
        "--metrics",
        "127.0.0.1:20241",
        "run",
      ]
    volumes:
      - ./deploy/public-ingress/cloudflared.yml:/etc/cloudflared/config.yml:ro
      - "${LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE:-/dev/null}:/etc/cloudflared/credentials.json:ro"
```

No container healthcheck: the image ships no shell, so readiness is observed
through the metrics endpoint from the shared namespace (acceptance, section
6) and through the sidecar log's registered edge connections. If the profile
is activated without a credentials path, cloudflared exits immediately naming
the unparseable `/etc/cloudflared/credentials.json` — loud, not degraded.

`deploy/public-ingress/cloudflared.yml`:

```yaml
# The tunnel UUID is an identifier, not a secret; the secret is the
# credentials JSON, which never enters the repo. Re-mint it with
#   cloudflared tunnel create lazycloud-control-plane
# and write the resulting UUID here once.
tunnel: <TUNNEL_UUID>
credentials-file: /etc/cloudflared/credentials.json

originRequest:
  connectTimeout: 10s
  noTLSVerify: false
  disableChunkedEncoding: false

ingress:
  # Ordered; first match wins; path is an anchored Go regex.
  # Internal-only surfaces are refused before the origin sees the request.
  - hostname: lazycloud.dev
    path: ^/metrics$
    service: http_status:404
  - hostname: lazycloud.dev
    path: ^/worker-repository(/|$)
    service: http_status:404
  - hostname: lazycloud.dev
    service: http://127.0.0.1:9000
  # Generated invoke hosts (<stub>.lazycloud.dev): all paths forward; the
  # control plane's host-routing middleware resolves the stub.
  - hostname: "*.lazycloud.dev"
    service: http://127.0.0.1:9000
  - service: http_status:404
```

Environment variables, complete list:

| Variable | Default | Where set |
| --- | --- | --- |
| `LAZYCLOUD_CLOUDFLARED_IMAGE` | `cloudflare/cloudflared:2026.7.3` | compose interpolation only (new) |
| `LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE` | `/dev/null` | compose interpolation (new); deployment `.env` points it at the minted credentials JSON, mode 0400 |
| `COMPOSE_PROFILES` | unset | deployment `.env`: `public-ingress` |
| `LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL` | `http://127.0.0.1:${LAZYCLOUD_COMPOSE_CONTROL_PLANE_PORT:-8000}` (`compose.yaml:23`, unchanged) | deployment `.env`: `https://lazycloud.dev` |
| `LAZYCLOUD_PUBLIC_INGRESS_CLIENT_IP_HEADER` | `""` (`compose.yaml:48`, unchanged) | deployment `.env`: `CF-Connecting-IP` |

No new Python settings; the two API-side settings already exist
(`apps/api/src/api/settings.py:48-67`).

One-time mint (owner, documented in `deploy/public-ingress/README.md`):

```sh
cloudflared tunnel login                                # owner's account
cloudflared tunnel create lazycloud-control-plane       # writes <UUID>.json
# Inspect existing zone DNS before mutation — the zone carries records from
# the superseded architecture; repointing the apex is the cutover.
cloudflared tunnel route dns lazycloud-control-plane lazycloud.dev
# Wildcard record via dashboard/API: proxied CNAME  *  ->  <UUID>.cfargotunnel.com
# (proxied wildcards are available on all plans since 2023)
mkdir -p ~/.lazycloud/public-ingress
mv ~/.cloudflared/<UUID>.json ~/.lazycloud/public-ingress/credentials.json
chmod 0400 ~/.lazycloud/public-ingress/credentials.json
# Then: write <UUID> into deploy/public-ingress/cloudflared.yml, set the four
# .env values above, and delete the OLD tunnel + rotate the legacy
# CLOUDFLARE_* credentials (INFRA-18) — the old token is compromised-by-
# handling and is never reused.
```

## 4. INFRA-07 — refuse managed capacity against an unreachable public origin

The composition site already exists and both processes already funnel through
it: `validate_provider_network_configuration`
(`packages/networking/src/networking/settings.py:152-210`), called with
`gateway_origin=gateway_config.public_http_url` from the API at
`apps/api/src/api/server/services.py:774-787` (origin at `:779`), from the
scheduler at `apps/scheduler/src/scheduler_app/services.py:231-243` (origin at
`:236`), per provider record via `configured_compute_provider_registry`
(`packages/provider-clients/src/provider_clients/factory.py:138-146`), and for
the AWS connection components
(`packages/provider-clients/src/provider_clients/aws_connections.py:563-570`).
It runs exactly when the AWS connection is configured — the resolver is built
only under `aws_account_connection_config.configured`
(`services.py:770-787`) — which is INFRA-07's intended gate.

The HTTPS requirement **already exists**: `_is_https_origin` rejects a
non-HTTPS public origin for the Remote class (`settings.py:164-166,
213-225`). What is missing is the unreachable-host refusal and the variable
name in the message. The change, confined to `settings.py`:

- After the HTTPS check, parse `urlparse(gateway_origin).hostname`; when
  `host_is_unreachable_from_a_remote_machine(host)` — the existing predicate,
  already imported at `settings.py:8`, defined at
  `packages/compute/src/compute/agent_control.py:919-936` — append the issue
  `public gateway origin host {host!r} is unreachable from a remote machine;
  set LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL to the public ingress origin`.
- Extend the existing HTTPS issue text to name
  `LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL` the same way, so the operator-facing
  refusal names the knob in both shapes.

No second predicate, no new call site, no change in
`agent_control.py` or either `services.py` — plan/13's file list for INFRA-07
predates the `validate_provider_network_configuration` consolidation and is
corrected by this design. The predicate classifies loopback, private,
link-local, unspecified and single-label hosts as unreachable and exempts
tailnet CGNAT/ULA ranges (`agent_control.py:904-916`), which is exactly the
wanted behavior: `https://127.0.0.1:8000` and `https://control-plane` are
refused; `https://lazycloud.dev` passes.

Acceptance (unchanged from plan/13): with
`LAZYCLOUD_AWS_CONNECTION_ENABLED=true` and the default public origin,
`docker compose up control-plane` exits non-zero naming
`LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL` and the offending host — and so does the
scheduler; with `https://lazycloud.dev` both start healthy.

## 5. INFRA-08 — delete Tailscale Serve and Funnel

Verified current references — the file's only consumer is the sidecar env and
mount:

- Delete `deploy/tailnet-gateway/serve.json` and the then-empty
  `deploy/tailnet-gateway/` directory. Its `AllowFunnel` block
  (`serve.json:16-18`) has never functioned — `deploy/tailnet/policy.json.tftpl`
  grants no `funnel` nodeAttr — and its TCP 443/Web handlers proxy to
  `127.0.0.1:9000`, a path no consumer uses (tailnet consumers dial port 9000
  directly, `compose.yaml:24`).
- Delete `TS_SERVE_CONFIG: "/config/serve.json"` — now at
  **`compose.yaml:563`** (plan/13 said `:561`; the file moved).
- Delete the `./deploy/tailnet-gateway:/config:ro` mount — now at
  **`compose.yaml:573`** (plan/13 said `:571`).

Nothing else depends on them: `grep -rnF 'serve.json' --exclude-dir=.git .`
matches only `compose.yaml` and the plan documents. (A regex grep with an
unescaped dot also matches `preserve_json` in
`packages/lazycloud/tests/test_sdk_cli_output.py:38` — a false positive; use
`-F`.) `TS_SERVE_CONFIG` and `AllowFunnel` appear nowhere else.

Land it with the first stack cycle after INFRA-06 (the sidecar restarts
anyway). Acceptance: `docker compose up -d tailnet-gateway` reaches
`BackendState: Running`; `tailscale --socket=/var/run/tailscale/tailscaled.sock
serve status` reports no configuration; a managed node still reaches
`http://<control-plane>.<tailnet>:9000/health`;
`grep -rnF "serve.json" --exclude-dir=.git .` and
`grep -rn "TS_SERVE_CONFIG\|AllowFunnel" --exclude-dir=.git .` return only
plan documents.

## 6. Acceptance

All commands run against the deployed connected stack; the EC2 vantage point
is an instance in the test customer account with **no tailnet session**.

Sidecar up and connected:

```sh
docker compose ps public-ingress
docker compose logs public-ingress | grep -c "Registered tunnel connection"   # expect >= 2
# readiness probe through the shared namespace (no shell in the cloudflared image):
docker compose exec control-plane python -c \
  "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:20241/ready', timeout=3).read().decode())"
```

Public reachability and TLS, from the EC2 instance:

```sh
curl -fsS https://lazycloud.dev/health
curl -fsS -o /dev/null -w '%{http_version} %{ssl_verify_result}\n' https://lazycloud.dev/health   # expect 2 0
```

`CF-Connecting-IP` arrives and the limiter buckets by it, not by the tunnel's
address (every tunneled request's socket peer is `127.0.0.1`, so per-address
bucketing working at all *is* the header working). `/auth/device` allows
10/min per address (`rate_limit.py:32`):

```sh
# From EC2 instance A: 201 x10, then 429 with Retry-After.
for i in $(seq 1 11); do curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST https://lazycloud.dev/auth/device \
  -H 'content-type: application/json' -d '{"client_name":"acc"}'; done
# From instance B (different public IP) in the same minute: still 201 —
# proves per-client bucketing rather than one shared tunnel bucket.
# Forgery cannot escape the bucket: from A, immediately after the 429,
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://lazycloud.dev/auth/device \
  -H 'CF-Connecting-IP: 198.51.100.7' \
  -H 'content-type: application/json' -d '{"client_name":"acc"}'   # expect 429:
# Cloudflare overwrote the forged header at the edge.
```

A refused prefix is refused from the public internet — and the 404 is the
edge's, not the app's (the loopback contrast returns the app's auth error,
proving the route is alive and the refusal happened in front of it):

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://lazycloud.dev/metrics                       # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://lazycloud.dev/worker-repository/get-worker-by-id                                    # 404
docker compose exec control-plane python -c \
  "from urllib.request import urlopen; from urllib.error import HTTPError
try: urlopen('http://127.0.0.1:9000/metrics', timeout=3)
except HTTPError as e: print(e.code)"                                                          # 401/403
```

Streams survive the 100 s edge window (requires the `_follow_task_logs`
heartbeat companion change):

```sh
curl -N -H "Authorization: Bearer $TOKEN" \
  'https://lazycloud.dev/api/v1/logs/stream?task_id=<idle-task>&follow=true'
# expect ": heartbeat" comments every <=15 s and the connection alive past 200 s
```

Then the track-level proof: `python -m tests.e2e.external.aws.one_machine_readiness`
to `ready: 1` once BOOT-04 lands, per `plan/13-production-readiness.md` track
acceptance 2–3.

## 7. Cutover order

Current state: Phase 2 (`plan/final.md`) is complete, so Gate A is satisfied
and INFRA-06 may start. Order, with what breaks when violated:

1. **Owner one-time** (before any code lands): inspect the existing
   `lazycloud.dev` zone records, mint the new tunnel, create the apex and
   wildcard DNS records, place the credentials file, then delete the old
   tunnel and rotate the legacy `CLOUDFLARE_*` credentials (INFRA-18).
   Repointing the apex is externally visible; the prior architecture behind it
   is already dead (`plan/final.md`, INFRA-21 notes).
2. **INFRA-06** lands: compose service, `cloudflared.yml`, docs, the
   logs-stream heartbeat companion, and the deployment `.env` values. A local
   stack is byte-for-byte unaffected (profile inactive, both compose defaults
   unchanged at `compose.yaml:23, 48`).
3. **INFRA-07** lands. From this commit a connected stack refuses to start
   without a real public origin — so the acceptance host's `.env` must carry
   the section-3 values in the same change, or the connected stack is down
   until it does. That coupling is the feature.
4. **BOOT-04** (nodes point at the public origin) — only after 2 and 3, and
   both composition sites in one commit
   (`apps/api/src/api/server/services.py:789-802`,
   `apps/scheduler/src/scheduler_app/services.py:249-264`, currently passing
   the runtime callback origin as `control_plane_url`). Done before INFRA-06:
   every launched node curls an origin that does not exist and the pool never
   launches — with INFRA-07 in place the stack refuses to start instead, which
   is the cheap failure; without it, the failure surfaces twenty minutes into
   an EC2 boot (`networking/settings.py:167-170`).
5. **BOOT-05 → BOOT-08 → BOOT-06 → BOOT-07 → BOOT-09** in `plan/final.md`
   Phase 4 order. BOOT-05 before BOOT-04 would pipe the installer from an
   origin a pre-join node cannot reach — all managed launches fail; the
   dependency annotations already forbid it.
6. **INFRA-08** any time after INFRA-06 — the Serve/Funnel path is inert, so
   nothing breaks out of order; keep it with the first post-INFRA-06 stack
   cycle to fold the sidecar restart in. INFRA-08 must not reach into
   `deploy/tailnet/` — BOOT-07 owns that directory
   (`plan/13-production-readiness.md`, dependencies).

Standing hazard through all of it: any `control-plane` recreate kills both
sidecars; every recreate is followed by
`docker compose up -d tailnet-gateway public-ingress` and a check that the
tunnel log shows registered connections and `tailscale status` reports
`Online: True`.
