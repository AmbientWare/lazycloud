# Production Readiness: Ingress, Exposure and Operability

## Target state

One public HTTPS hostname on `lazycloud.dev` serves the whole control plane —
`/api/v1/*`, `/gateway/*`, `/install/agent`, `/health`, and the dashboard — over
a `cloudflared` sidecar that runs in the control plane's network namespace
exactly as `tailnet-gateway` does today. Nothing listens on a public port; the
sidecar dials out. TLS terminates at Cloudflare's edge, so the deployment holds
no certificate and no ACM/ALB/Route53 resources.

The tailnet carries only the data path: the control plane dialing an enrolled
agent's route proxy. Tailscale Serve and Funnel are gone — `serve.json` is
deleted, not disabled.

Every unauthenticated route is rate-limited in-process against Redis before it
touches Postgres or AWS. `/gateway/provider-nodes/*` no longer makes a live AWS
call per request: it reads the durable provider inventory the reconciler already
refreshes, and the presigned-STS verification runs under a bounded concurrency
limiter so a flood is rejected in microseconds instead of parking a threadpool
worker for three seconds.

A failed node is knowable without SSH: it pushes a bounded diagnostic excerpt
with its failure report, and the operator role can pull `ec2:GetConsoleOutput`
and SSM `AWS-RunShellScript` against nodes tagged `cloud-pool:managed-by=control-plane`.

Prometheus scrapes `/metrics`, Alertmanager pages on the specific failure
signals the other three tracks make observable, and the capacity/enrolment error
paths write durable `events` rows instead of swallowing.

No platform-issued long-lived secret exists inside a customer AWS account.

## Why this is simpler and more reliable

A tunnel sidecar is the shape the repo already runs and already documents the
hazards of (`deploy/AGENTS.md:44-49`): one container, one namespace, one
outbound connection, no inbound port, no certificate lifecycle, no load
balancer, no VPC. An ALB is not an alternative to that — it is a prerequisite
migration, because the control plane runs under `docker compose` on an operator
host today (`compose.yaml:437-506`) and nothing in the repo deploys it into AWS.

Tailscale Funnel was already the wrong answer twice over: it failed under load
during validation, and it cannot work at all as checked in — `serve.json:16-18`
requests `AllowFunnel`, and `deploy/tailnet/policy.json.tftpl` grants no `funnel`
nodeAttr, so the request is inert. Deleting it removes a config that looks like
an ingress and is not one.

Removing the live AWS describe from the enrolment path removes a whole class of
problem rather than throttling it: today one unauthenticated POST costs an
`sts:AssumeRole` into a customer account, a `DescribeAutoScalingGroups`, a
Postgres write, and a 3 s outbound STS fetch, all inside one threadpool worker.
Reading the inventory the reconciler already maintains makes the request cost a
single indexed SELECT, and the rate limiter then has something cheap to protect.

Diagnosability needs almost no new machinery: the node role already carries the
full SSM agent grant (`packages/providers/aws/src/provider_aws/account_connection.py:1787-1828`,
applied by `_ensure_node_diagnostics_policy` at `:1834-1855`), and
`deploy/connected-aws/operator-ssm-diagnostics-policy.json` is the exact
operator-side counterpart — written, correct, and referenced by nothing. Wiring
a file that already exists is cheaper than inventing a log-shipping pipeline.

## Dependencies on other tracks

From **10-bootstrap-enrolment.md**:

- **BOOT-04** ("Point managed-pool nodes at the public control-plane origin")
  consumes INFRA-06 (the ingress) and is gated on INFRA-03/04/05 (the
  amplification fix and the limiter). BOOT-04 states this dependency itself
  (`plan/10-bootstrap-enrolment.md:213-215`). Nothing in this track depends on
  BOOT-04 landing first.
- **BOOT-03** ("Report bootstrap phases from the agent") must land before
  INFRA-13, which extends the bootstrap-failure contract in
  `packages/shared/src/shared/http/provider_nodes.py` — the same file BOOT-03
  changes. Sequence INFRA-13 after BOOT-03 to avoid a conflicting edit.
- **BOOT-06** ("Delete the pool bootstrap key subsystem") is the fix for the
  plaintext pool key. This track does **not** duplicate it. INFRA-17 covers only
  the residue BOOT-06 leaves behind: keys already minted at Tailscale, and copies
  already written into customer launch-template versions.
- **BOOT-07** owns every file under `deploy/tailnet/`. This track touches
  `deploy/tailnet-gateway/` only. **BOOT-08** owns `deploy/ami/bake.py:614-660`;
  INFRA-12 adds one assertion to the same bake script and must land after it.

From **11-capacity-scheduling.md**:

- **CAP-02** ("Carry a typed reason on every transition to `Unavailable`") and
  **CAP-03** ("Report which registration step failed") produce the signals
  INFRA-16 alerts on. INFRA-16 is worth building before them but its rules are
  not complete until they land.
- **CAP-08** ("Introduce one owned readiness predicate") gives INFRA-16 the one
  correct definition of "a machine that should be ready and is not". Write the
  alert against that predicate, not against `enrollment.readiness_phase`.
- INFRA-03 changes what `/gateway/provider-nodes/*` reads about ASG membership.
  If CAP-15 (`Delete CapacityPoolSizingState`) or CAP-13 changes the durable
  provider-instance inventory shape, INFRA-03 reads whatever CAP leaves as the
  authority. Confirm with the CAP track before implementing INFRA-03.

From **12-failure-semantics.md** (ERR-*): not yet written at the time of this
plan. INFRA-15 (route error paths through the durable event channel) is the
delivery mechanism for whatever taxonomy ERR defines; if ERR-* introduces a
typed failure contract, INFRA-15 emits that type rather than an ad-hoc string.
Coordinate before implementing INFRA-15.

## Work items

- [ ] **INFRA-01** Add a Redis rate-limiter primitive to coordination
  - **Files**: new file: `packages/coordination/src/coordination/rate_limit.py`
  - **Change**: implement a fixed-window counter and a bounded-concurrency lease
    over `RedisClient`, in the style of
    `packages/coordination/src/coordination/token_lock.py`. Two functions:
    `try_consume(redis, key, *, limit: int, window_seconds: int) -> bool` using a
    single Lua script through `redis.eval_int` (`INCR` then `EXPIRE` only on the
    first increment in the window, so the TTL is never extended by later hits),
    and `try_acquire_slot(redis, key, token, *, limit: int, ttl_seconds: int) -> bool`
    plus `release_slot(redis, key, token)` for bounded in-flight concurrency,
    fenced by token exactly as `release_token_lock` is. Raise `ValueError` on
    non-positive `limit`/`window_seconds`/`ttl_seconds`. No settings, no
    middleware, no HTTP awareness in this module.
  - **Risk**: a wrong TTL rule turns the limiter into a permanent block (window
    key never expires) or into no limiter at all (TTL reset on every hit). Blast
    radius is confined until INFRA-05 wires it.
  - **Acceptance**: against the Compose Redis, a loop of 100 `try_consume` calls
    with `limit=10, window_seconds=1` returns exactly 10 `True` in the first
    window and 10 more after the window rolls; `TTL` on the key is observed to
    stay ≤ 1 s across the burst rather than climbing. A `try_acquire_slot`
    holder that never releases frees its slot after `ttl_seconds`.
  - **Depends on**: none

- [ ] **INFRA-02** Resolve the client address from exactly one configured source
  - **Files**: new file: `apps/api/src/api/server/client_address.py`;
    `apps/api/src/api/settings.py`; `compose.yaml:37-47` (the `x-api-env` block)
  - **Change**: add `LAZYCLOUD_PUBLIC_INGRESS_CLIENT_IP_HEADER` (default `""`) to
    the API settings. Implement `client_address(scope, *, header_name: str) -> str`:
    when `header_name` is empty the client is the ASGI `scope["client"]` peer —
    there is no proxy, so the peer *is* the client; when `header_name` is set,
    the client is the last value of that header and the socket peer is ignored.
    Never read `X-Forwarded-For` unless it is the configured name. Return `""`
    when the configured header is absent or unparseable as an IP, and have
    callers treat `""` as one shared bucket rather than as "unlimited". Add the
    variable to `compose.yaml` with an empty default.
  - **Risk**: trusting a client-settable header without an ingress in front lets
    any caller forge its rate-limit bucket. The empty default is what prevents
    that; the deployment sets it only because INFRA-06 puts a proxy in front.
  - **Acceptance**: with the variable empty, a request carrying
    `X-Forwarded-For: 1.2.3.4` is bucketed under the socket peer. With it set to
    `CF-Connecting-IP`, the same request is bucketed under the socket peer and a
    request carrying `CF-Connecting-IP: 1.2.3.4` is bucketed under `1.2.3.4`.
  - **Depends on**: none

- [ ] **INFRA-03** Stop calling AWS inside the provider-node request path
  - **Files**: `packages/gateway/src/gateway/provider_enrollment.py:184-215`;
    `packages/compute/src/compute/service.py:2417-2433`
  - **Change**: `_verify_active_node` currently calls
    `self.compute.describe_internal_pool(...)` (`provider_enrollment.py:195-198`),
    which reaches `provider.pooled.describe_pool` (`service.py:2425`) — a live
    `sts:AssumeRole` into the customer account plus `DescribeAutoScalingGroups`,
    followed by a durable snapshot write (`service.py:2426-2432`), on **every
    unauthenticated request**. Replace it with a read of the durable
    provider-instance inventory (`ComputeProviderInstanceRepository.list_for_pool`,
    already imported at `provider_enrollment.py:13`), which the AWS capacity
    reconciler refreshes every 30 s
    (`LAZYCLOUD_AWS_CAPACITY_RECONCILIATION_INTERVAL_SECONDS`, `compose.yaml:125`).
    Keep the pool-identity check by comparing `pool.provider_state.resource_id`
    against the stored value rather than a live snapshot. When the claimed
    `provider_instance_id` is absent from the inventory, request **one**
    reconciliation refresh for that pool guarded by
    `try_consume(..., limit=1, window_seconds=5)` keyed on the pool id, then
    re-read once; if it is still absent, raise the existing
    `IdentityMismatch`-equivalent `InvalidInputError`. A caller that cannot get
    past the limiter gets `UpstreamUnavailableError` (503), never a live AWS call.
  - **Risk**: an instance that enrols faster than the reconciler observes it now
    waits up to one refresh instead of succeeding immediately, which lengthens
    node bring-up. The 5 s refresh window bounds that. Blast radius: every
    managed-pool enrolment; get this wrong and no node enrols. Confirm the
    inventory shape with the CAP track first — CAP-13/CAP-15 rework neighbouring
    durable state.
  - **Acceptance**: with the AWS SDK's endpoint pointed at an unroutable address
    inside the control-plane container, `POST /gateway/provider-nodes/bootstrap-phase`
    for a known-good instance still succeeds — proving no AWS call is on the
    path. Then run `python -m tests.e2e.external.aws.one_machine_readiness` to
    `ready: 1` and confirm from CloudTrail (or from
    `aws cloudformation`/`autoscaling` call counts) that
    `DescribeAutoScalingGroups` is called on the reconciler cadence, not per
    report.
  - **Depends on**: INFRA-01

- [ ] **INFRA-04** Bound the STS proof verification so it cannot exhaust the API
  - **Files**: `apps/api/src/api/server/routers/gateway/agents.py:45-78`;
    `packages/providers/aws/src/provider_aws/provider_node_identity.py:29, 298-308`;
    `packages/gateway/src/gateway/provider_enrollment.py:205-215`
  - **Change**: the three provider-node routes are `def`, not `async def`
    (`agents.py:50, 62, 74`), so FastAPI runs them on the anyio threadpool
    (40 threads by default) and each holds a thread for up to
    `AWS_STS_PROOF_TIMEOUT_SECONDS = 3.0`
    (`provider_node_identity.py:29`). Forty concurrent unauthenticated requests
    therefore stall **every** synchronous route in the process. Fix both halves:
    (a) wrap the `identity_verifier.verify(...)` call in
    `_verify_active_node` in a `try_acquire_slot` lease keyed
    `provider-node-proof` with a configured limit (default 8) and a TTL of
    `AWS_STS_PROOF_TIMEOUT_SECONDS + 1`; on refusal raise
    `UpstreamUnavailableError("identity verification is at capacity")` before any
    outbound call, and release the slot in a `finally`; (b) lower
    `AWS_STS_PROOF_TIMEOUT_SECONDS` to 2.0 and give the HTTP adapter an explicit
    connect timeout so a black-holed STS endpoint cannot hold the full budget.
    Add `LAZYCLOUD_PROVIDER_NODE_PROOF_MAX_INFLIGHT` to the API settings and to
    `compose.yaml`.
  - **Risk**: too low a limit throttles a genuine simultaneous scale-out. Eight
    concurrent proofs at ≤2 s each is ~4 enrolments/second sustained, well above
    any real ASG launch rate. Blast radius: enrolment latency under burst.
  - **Acceptance**: with the control plane running, fire 200 concurrent
    `POST /gateway/provider-nodes/enroll` with a syntactically valid but
    unresolvable proof URL; observe that `GET /health` still answers under
    200 ms throughout, that at most 8 outbound STS connections exist at any
    instant (`ss -tn state established '( dport = :443 )'` inside the container),
    and that excess requests return 503 immediately rather than after 3 s.
    Repeat against the unfixed build to record the contrast.
  - **Depends on**: INFRA-01, INFRA-03

- [ ] **INFRA-05** Rate-limit every unauthenticated route before exposure
  - **Files**: new file: `apps/api/src/api/server/rate_limit.py`;
    `apps/api/src/api/fastapi_app.py:209-217`; `apps/api/src/api/settings.py`;
    `compose.yaml` (`x-api-env`)
  - **Change**: add an ASGI middleware registered **outermost** — before
    `GatewayRequestEventMiddleware` (`fastapi_app.py:213`), so a rejected request
    never reaches the 5xx durable-event writer and cannot amplify into Postgres.
    It applies a per-client-address fixed window (INFRA-01, keyed by INFRA-02's
    resolver) and a separate global window, to exactly these path prefixes, which
    are the complete set of routes with no authentication dependency:
    `/gateway/provider-nodes/` (`agents.py:45-78`), `/auth/device`
    (`apps/api/src/api/server/routers/system.py:285`, which creates a durable
    `DeviceAuthorizationRecord` per call), `/auth/device/token` (`system.py:309`),
    `/auth/authorize` (`system.py:379`), and `/health` (`system.py:83`, which
    pings Postgres and Redis per call). Return `429` with a `Retry-After` header
    and no body detail that reveals the limit. Defaults, all settable:
    30 req/min per address and 600 req/min global for the provider-node prefix;
    10/min per address for `/auth/device*`; 60/min per address for `/health`.
    Add a second, independent per-pool window inside
    `ProviderNodeEnrollmentService._verify_active_node`, keyed on
    `enrollment_request_id`, so a single valid pool UUID cannot be used to drive
    unlimited work regardless of source address.
  - **Risk**: an over-tight `/health` limit breaks the Compose healthcheck
    (`compose.yaml:501-508`) and the ingress's own probe — both come from the
    same address, so size the per-address limit above their combined cadence.
    A limiter that fails closed when Redis is unavailable takes the control
    plane down; make a Redis error fail **open** for `/health` and closed for the
    provider-node prefix, and log the decision.
  - **Acceptance**: `for i in $(seq 1 60); do curl -s -o /dev/null -w '%{http_code}\n' <origin>/auth/device -X POST -d '{"client_name":"x"}' -H 'content-type: application/json'; done`
    returns `201` up to the limit and `429` after, with `Retry-After` present;
    `SELECT count(*) FROM device_authorizations` grows by the limit, not by 60.
    The Compose stack reports `control-plane` healthy for 10 minutes with the
    limiter enabled.
  - **Depends on**: INFRA-01, INFRA-02

- [ ] **INFRA-06** Stand up the public HTTPS ingress
  - **Files**: new files: `deploy/public-ingress/cloudflared.yml`,
    `deploy/public-ingress/README.md`, `deploy/public-ingress/AGENTS.md` (+ its
    `CLAUDE.md` symlink); `compose.yaml` (new `public-ingress` service, and
    `LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL` at `:23`)
  - **Change**: add a `public-ingress` service running
    `cloudflare/cloudflared` with `network_mode: "service:control-plane"` and
    `command: ["tunnel", "--no-autoupdate", "--config", "/config/cloudflared.yml", "run"]`,
    mounting `./deploy/public-ingress:/config:ro` and a credentials file from
    `${LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE}` (never committed; the tunnel
    secret is a secret). `cloudflared.yml` declares the tunnel id, the
    credentials path, and one ingress rule mapping the public hostname to
    `http://127.0.0.1:9000` plus a catch-all `http_status: 404`. Set
    `originRequest.connectTimeout: 10s`, `noTLSVerify: false`, and
    `disableChunkedEncoding: false` so SSE (`/gateway/agents/stream/events`,
    `/api/v1/logs/stream`) streams. Point `LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL` at
    `https://<hostname>` in the deployment `.env`; leave the compose default as
    the loopback origin so a local stack without a tunnel is unchanged.
    Document in the new `AGENTS.md` the same namespace hazard `tailnet-gateway`
    has: recreating `control-plane` destroys this sidecar and Compose does not
    bring it back (`deploy/AGENTS.md:44-49`) — follow any `control-plane`
    recreate with `docker compose up -d public-ingress tailnet-gateway`.
  - **Risk**: this is the one item with an external dependency the repo cannot
    provision itself. The owner already holds the `lazycloud.dev` zone and a
    Cloudflare account (evidenced by `CLOUDFLARE_ZONE_ID` / `CLOUDFLARE_API_KEY`
    / `CLOUDFLARE_TUNNEL_TOKEN` in the untracked
    `infrastructure/secrets-backup/` from the prior architecture) — **verify this
    before starting**, because if it is wrong the item becomes a domain and CDN
    procurement task. Cloudflare's free plan caps a request body at 100 MB;
    confirm no control-plane route carries a larger body (artifacts and image
    archives use presigned object-store URLs, `compose.yaml:59`, so they should
    not transit the API — prove it rather than assume it). A 100 s edge timeout
    applies per request; long polls must stream, not block.
  - **Acceptance**: from an EC2 instance in the test customer account with **no
    tailnet session**, `curl -fsS https://<hostname>/health` returns the health
    JSON, and `curl -fsS -o /dev/null -w '%{http_version} %{ssl_verify_result}\n' https://<hostname>/health`
    reports a verified TLS chain. `curl -N https://<hostname>/api/v1/logs/stream?...`
    with a valid token delivers at least two SSE frames more than 100 s apart.
    `docker compose ps` shows `public-ingress` up and `cloudflared` reports at
    least two healthy edge connections in its log.
  - **Depends on**: INFRA-03, INFRA-04, INFRA-05

- [ ] **INFRA-07** Refuse to launch managed capacity against an unreachable public origin
  - **Files**: `packages/compute/src/compute/agent_control.py:896-921`;
    `apps/api/src/api/server/services.py:765-798`;
    `apps/scheduler/src/scheduler_app/services.py:249-276`
  - **Change**: `_reject_unroutable_runtime_url` (`agent_control.py:896-921`)
    already refuses a runtime callback origin a remote machine cannot resolve.
    Add the same refusal for the **public** origin on the managed-pool path:
    when the AWS connection is configured
    (`aws_account_connection_config.configured`, `services.py:781`) and
    `gateway_config.public_http_url` resolves to loopback, a Compose service
    name, or any host matching `host_is_unreachable_from_a_remote_machine`,
    raise at composition time with a message naming
    `LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL` and the offending host. Reuse the
    existing predicate; do not add a second one. Additionally require `https`
    for the public origin whenever the AWS connection is configured.
  - **Risk**: a deployment that legitimately runs the control plane and its nodes
    on the same host would now be refused. No such deployment exists — managed
    capacity is EC2 by definition. Blast radius: startup of a misconfigured
    connected stack, which is the intent.
  - **Acceptance**: with `LAZYCLOUD_AWS_CONNECTION_ENABLED=true` and
    `LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL=http://127.0.0.1:8000`,
    `docker compose up control-plane` exits non-zero with a message naming the
    variable and `127.0.0.1`; with the real ingress origin it starts healthy.
  - **Depends on**: INFRA-06

- [ ] **INFRA-08** Delete Tailscale Serve and Funnel
  - **Files**: delete `deploy/tailnet-gateway/serve.json`;
    `compose.yaml:561` and `:571`
  - **Change**: remove `TS_SERVE_CONFIG` (`compose.yaml:561`) and the
    `./deploy/tailnet-gateway:/config:ro` mount (`compose.yaml:571`), then delete
    the file and the now-empty `deploy/tailnet-gateway/` directory. The file's
    only consumer is that one env var. Its `AllowFunnel` block (`serve.json:16-18`)
    has never worked — `deploy/tailnet/policy.json.tftpl` grants no `funnel`
    nodeAttr — and its `TCP 443` / `Web` handlers proxy to `127.0.0.1:9000`, a
    path nothing uses: every tailnet consumer dials port 9000 directly
    (`deploy/tailnet/policy.json.tftpl:16, 20`, `compose.yaml:24`).
  - **Risk**: none identified beyond the sidecar restart. If any operator runbook
    or `.env` references the serve config, update it in the same change.
  - **Acceptance**: `docker compose up -d tailnet-gateway` reports
    `BackendState: Running`; `tailscale --socket=... serve status` reports no
    configuration; a managed node still reaches
    `http://<control-plane>.<tailnet>:9000/health` over the tailnet;
    `grep -rn "serve.json\|TS_SERVE_CONFIG\|AllowFunnel" .` returns nothing
    outside `.git`.
  - **Depends on**: INFRA-06

- [ ] **INFRA-09** Grant the operator role node diagnostics
  - **Files**: `deploy/connected-aws/control-stack.yaml:230-272`;
    `deploy/connected-aws/operator-ssm-diagnostics-policy.json`
  - **Change**: `deploy/connected-aws/operator-ssm-diagnostics-policy.json` is a
    complete, correctly scoped operator policy — `ssm:SendCommand` restricted to
    `AWS-RunShellScript` and to instances tagged
    `cloud-pool:managed-by=control-plane` — and **is referenced by nothing**
    (`grep -rn operator-ssm-diagnostics-policy .` finds only the file). Inline
    its three statements into the `OperatorRole` policy in
    `control-stack.yaml` as new `Sid`s, then delete the standalone JSON file so
    one owner states the policy. In the same statement block add
    `ec2:GetConsoleOutput` on `arn:aws:ec2:*:*:instance/*` — console output is
    the only diagnostic that survives an instance that never reached userland,
    where SSM cannot help. Do **not** add `ec2:DescribeLaunchTemplateVersions`
    until BOOT-06 removes the tailnet key from user-data; until then that grant
    would expose a live credential to the acceptance operator.
  - **Risk**: `ssm:SendCommand` is remote code execution on managed nodes. The
    tag condition confines it to platform-managed instances; verify the tag is
    applied at launch (`packages/providers/aws/src/provider_aws/managed_pool.py:1103`
    tags instances under `cloud-pool:*`) before relying on it. `GetConsoleOutput`
    can surface kernel messages containing instance metadata; treat its output as
    sensitive.
  - **Acceptance**: after `aws cloudformation update-stack --stack-name lazycloud-connected-aws-control`,
    `aws ssm describe-instance-information --profile default-test` succeeds where
    it currently returns `AccessDeniedException` for
    `ssm:DescribeInstanceInformation` (verified live on 2026-07-31), a running
    managed node appears with `PingStatus: Online`,
    `aws ssm send-command --document-name AWS-RunShellScript --targets Key=InstanceIds,Values=<node> --parameters commands='journalctl -u lazycloud-agent -n 200'`
    returns output, and the same command against an untagged instance is denied.
    `aws ec2 get-console-output --instance-id <node>` returns non-empty text.
  - **Depends on**: none

- [ ] **INFRA-10** Prove the SSM agent is enabled in the baked AMI
  - **Files**: `deploy/ami/bake.py:614-660`
  - **Change**: the bake starts from the latest AL2023 image
    (`bake.py:390-406`), which is documented upstream to ship
    `amazon-ssm-agent` enabled — but nothing in this repo asserts it, so INFRA-09
    rests on an assumption. **(Inferred, not read from this repo.)** In the bake
    user-data add `systemctl enable --now amazon-ssm-agent` and a hard
    `systemctl is-enabled amazon-ssm-agent` check that fails the bake, and record
    `"ssm_agent": true` in `/etc/lazycloud-node-image.json`
    (`bake.py:655-657`). Also redirect the node bootstrap's own output to a file
    the SSM shell can read: today `_BOOTSTRAP_SCRIPT_TEMPLATE`
    (`packages/compute/src/compute/node_bootstrap.py:173`) has no `exec >log`
    redirection, unlike the bake script (`bake.py:616`).
  - **Risk**: touches `deploy/ami/bake.py:614-660`, which BOOT-08 deletes and
    replaces with the published install script. Land this **after** BOOT-08 and
    apply the same two changes to whatever install path BOOT-08 leaves, or the
    edit is thrown away. Re-baking AMIs is a real cost (one bake per region).
  - **Acceptance**: a freshly baked AMI launched into the test account appears in
    `aws ssm describe-instance-information` within 5 minutes of boot, and
    `cat /etc/lazycloud-node-image.json` on that node shows `"ssm_agent": true`.
  - **Depends on**: INFRA-09, BOOT-08

- [ ] **INFRA-11** Emit HTTP request metrics from the gateway middleware
  - **Files**: `packages/gateway/src/gateway/events.py:35-93`;
    `packages/observability/src/observability/metrics.py:19-80`
  - **Change**: `GatewayRequestEventMiddleware`'s docstring asserts "request
    volume is observable through metrics and logs" (`events.py:36-41`); it is
    not — the middleware records no timing (`events.py:47-63`) and nothing else
    produces HTTP metrics. Add a `MetricsSink` protocol alongside the existing
    `GatewayEventSink` and, on every request, increment
    `http_requests_total{method,route,status_class}` and observe
    `http_request_duration_seconds{method,route}` using `time.monotonic()`
    around `await self.app(...)`. Label with the **route template**
    (`scope["route"].path` when present) not the raw path, or a high-cardinality
    path like `/api/v1/tasks/{id}` explodes the `metrics` table's unique index
    (`packages/database/src/database/tables/observability.py:207`). Keep the
    5xx-only durable event exactly as is. Correct the docstring.
  - **Risk**: cardinality. Every distinct label set is a durable row; an
    unbounded `route` label would grow the `metrics` table without limit. Cap by
    falling back to the literal string `"unmatched"` when no route template
    exists.
  - **Acceptance**: after 100 mixed requests, `GET /metrics` with an admin token
    shows `http_requests_total` split by status class and
    `http_request_duration_seconds_count` equal to the request count;
    `SELECT count(*) FROM metrics WHERE name LIKE 'http_%'` stays bounded by the
    number of distinct route templates across a 10-minute load run, not by
    request count.
  - **Depends on**: none

- [ ] **INFRA-12** Make `/metrics` scrapable and correctly typed
  - **Files**: `packages/observability/src/observability/metrics.py:65-80`;
    `apps/api/src/api/server/routers/system.py:112-124`
  - **Change**: `prometheus_text()` (`metrics.py:65-80`) emits no `# HELP` or
    `# TYPE` lines and renders histograms as `_count`/`_sum`/`_min`/`_max`/`_last`
    (`metrics.py:72-79`), which are not Prometheus histogram buckets — so
    `rate()` and `histogram_quantile()` over them are wrong. Emit `# TYPE` for
    every series (`counter`, `gauge`, and `summary` for the aggregate form, which
    is what the data actually is), and rename the aggregate suffixes to the
    summary convention so a scraper parses them. Keep the route admin-gated
    (`system.py:116`) — the scraper carries an admin token; do not add an
    unauthenticated metrics endpoint.
  - **Risk**: renaming series breaks any existing dashboard. There are none
    (no Grafana JSON, no alert rules anywhere in the repo), so this is the
    cheapest moment to fix the naming.
  - **Acceptance**: `promtool check metrics < <(curl -sH "Authorization: Bearer $ADMIN" <origin>/metrics)`
    exits 0 with no warnings.
  - **Depends on**: INFRA-11

- [ ] **INFRA-13** Carry a bounded diagnostic excerpt on the bootstrap-failure report
  - **Files**: `packages/shared/src/shared/http/provider_nodes.py:44-55`;
    `packages/gateway/src/gateway/provider_enrollment.py:132-156`;
    the agent-side reporter BOOT-03 introduces
  - **Change**: a failed node today reports one enum value
    (`MachineBootstrapFailureReason`, `provider_nodes.py:55`) and nothing else —
    the prior branch recorded that this named *what* failed but never *why*
    (`plan/00-prior-tailnet-plan.md`, "Constraints that bit already"). Add
    `diagnostic_excerpt: str = Field(default="", max_length=8192, repr=False)` to
    `ProviderNodeBootstrapFailureRequest`. The reporter fills it with the last
    8 KiB of the install log. Server side, accept it **only after**
    `_verify_active_node` succeeds (`provider_enrollment.py:137-144`) so it is
    never attacker-controlled storage, strip control characters, and persist it
    on the provider-instance payload alongside `bootstrap_failure_reason` plus
    one `EventLevel.Error` durable event via `EventService.emit`
    (`packages/observability/src/observability/events.py:36-58`) with
    `resource_type="provider-instance"`. Never log it at INFO; a bootstrap log
    can contain a credential.
  - **Risk**: 8 KiB × every failing node is durable storage growth; the `events`
    30-day retention (`events.py:27`) bounds it. Redaction is not guaranteed —
    treat the field as sensitive everywhere it is rendered. This file is also
    edited by BOOT-03; land after it.
  - **Acceptance**: break the agent artifact URL for one launch; the node's
    failure lands with a non-empty excerpt naming the download error, readable
    from `compute_provider_instances.payload` and from
    `GET /api/v1/events?resource_type=provider-instance` — with no SSH session
    and no console read. Confirm the excerpt is truncated, not rejected, when the
    log exceeds 8 KiB.
  - **Depends on**: BOOT-03

- [ ] **INFRA-14** Route capacity and enrolment error paths through the durable event channel
  - **Files**: `packages/gateway/src/gateway/provider_enrollment.py:106-116`;
    `packages/compute/src/compute/service.py` (managed-pool error handlers);
    `packages/scheduler/src/scheduler/autoscaling.py`;
    `apps/api/src/api/fastapi_app.py:282-283, 304-305`
  - **Change**: there are 338 broad `except Exception` / `except BaseException` /
    bare handlers across `packages/` and `apps/`; exactly **2** emit a durable
    event (`packages/gateway/src/gateway/service.py:562-563` and
    `packages/execution/src/execution/tasks.py:607-608`), 39 log, and 178 neither
    log nor re-raise. Do **not** audit all 338 — that is a multi-week pass with
    no bounded outcome. Scope this item to the capacity and enrolment path only:
    the compensating rollback in `ProviderNodeEnrollmentService.enroll`
    (`provider_enrollment.py:106-116`, which suppresses two exceptions and
    records nothing), the two reconciliation loops in `fastapi_app.py:282-283`
    and `:304-305` (which log and continue), and the managed-pool and autoscaler
    handlers. Each gains one `EventService.emit(..., level=EventLevel.Error)`
    naming the pool and the operation. Add a line to
    `packages/observability/AGENTS.md` (create it, with the `CLAUDE.md` symlink)
    stating the rule: a broad handler on a capacity, enrolment, or billing path
    emits a durable event or re-raises — it does not swallow.
  - **Risk**: emitting inside a handler that is itself running because the
    database is unavailable will raise again. Wrap each emit so a failure to
    record never replaces the original failure, and re-raise the original.
  - **Acceptance**: force the `bind_machine` step in `enroll` to fail (point the
    repository at a revoked machine id); the run leaves a durable
    `EventLevel.Error` row naming the pool and the compensating rollback, where
    today it leaves nothing. Confirm the original exception still propagates
    unchanged.
  - **Depends on**: INFRA-13; coordinate with 12-failure-semantics.md (ERR-*)

- [ ] **INFRA-15** Scrape, alert, and page on the tracks' failure signals
  - **Files**: new files: `deploy/monitoring/prometheus.yml`,
    `deploy/monitoring/rules/lazycloud.yml`,
    `deploy/monitoring/alertmanager.yml`, `deploy/monitoring/AGENTS.md` (+
    `CLAUDE.md` symlink); `compose.yaml` (new `prometheus` and `alertmanager`
    services)
  - **Change**: add `prom/prometheus` and `prom/alertmanager` as Compose
    services on the internal network with no published ports (reach them through
    `docker compose exec` or an SSH tunnel; they must not join the public
    ingress). Prometheus scrapes `http://control-plane:9000/metrics` every 30 s
    with an admin bearer token supplied from the environment. Rules, each
    written against a signal another track makes observable:
    `ProviderNodeEnrolmentFailing` (`autoscaler_*` failure counter rising over
    15 m), `MachineNeverReady` (CAP-08's readiness predicate false for a machine
    older than the bootstrap deadline), `UnavailableWithReason` (CAP-02's typed
    reason counter, by reason), `ControlPlane5xx` (`http_requests_total` with
    `status_class="5xx"` over 5 m, from INFRA-11), `RateLimitSaturated`
    (429 rate sustained — a real attack or a limit set too low),
    `ReconciliationStalled` (AWS connection reconciliation failure count
    non-zero for 10 m, the signal `fastapi_app.py:296-303` currently only logs),
    and `ControlPlaneDown` (scrape failure). Alertmanager routes to one
    receiver — the owner's chosen webhook or email — with a 4 h repeat interval.
  - **Risk**: two more containers and a scraped admin token in the stack. Keep
    the token in the same protected `.env` as the rest and never publish the
    Prometheus port. Alert rules written before CAP-02/CAP-08 land will reference
    series that do not exist yet and fire nothing — mark those rules and enable
    them as their track lands rather than shipping silently broken rules. Cost:
    roughly one day to stand up, plus tuning.
  - **Acceptance**: `promtool check rules deploy/monitoring/rules/lazycloud.yml`
    exits 0; stopping `control-plane` produces a `ControlPlaneDown` notification
    at the configured receiver within the configured `for` window; driving 20
    consecutive 5xx responses produces `ControlPlane5xx`. Each rule is
    demonstrated firing at least once, not merely written.
  - **Depends on**: INFRA-12; rules for CAP signals depend on CAP-02, CAP-08

- [ ] **INFRA-16** Retire the pool bootstrap key material BOOT-06 leaves behind
  - **Files**: `packages/providers/aws/src/provider_aws/account_connection.py:1483-1493`;
    `packages/providers/aws/src/provider_aws/managed_pool.py:288-289, 755`;
    operator procedure recorded in `deploy/connected-aws/README.md` (create)
  - **Change**: BOOT-06 deletes the pool bootstrap key **subsystem**; it does not
    retire the key material already in the world. Two residues remain. First,
    every key ever minted still exists at Tailscale with a 90-day TTL
    (`packages/networking/src/networking/tailnet_control.py:28`) — enumerate keys
    described `pool bootstrap *` (`tailnet_control.py:397`) and revoke each,
    scoped to exactly those descriptions, preserving every unrelated key.
    Second, the key is base64-encoded into launch-template user-data
    (`managed_pool.py:1131`) in the **customer's** account, and every historical
    launch-template version retains its copy; the connection role grants
    `ec2:DescribeLaunchTemplateVersions` (`account_connection.py:1434`) but no
    `ec2:DeleteLaunchTemplateVersions` (`:1483-1493`), so the platform cannot
    prune them — only whole-template deletion clears them
    (`managed_pool.py:288-289`). Add `ec2:DeleteLaunchTemplateVersions` to the
    tag-conditioned `ManageTaggedLaunchTemplates` statement and have the pool
    reconciler delete every non-default version after a successful refresh.
  - **Risk**: **this changes the customer-facing IAM contract**, so it requires a
    new authorization generation and every connected customer must re-run the
    CloudFormation template — a customer-visible migration, not a code change.
    That is why it is separate from BOOT-06. Deleting launch-template versions is
    irreversible; scope by the `cloud-pool:managed-by=control-plane` tag and never
    delete the default version.
  - **Acceptance**: after apply, for the test connection,
    `aws ec2 describe-launch-template-versions --launch-template-id <id>` returns
    exactly one version, and
    `aws ec2 describe-launch-template-versions --launch-template-id <id> --versions '$Latest' --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text | base64 -d | grep -c tskey-`
    returns 0. The Tailscale key list contains no key described `pool bootstrap *`,
    and every unrelated key is still present (compare a before/after inventory).
  - **Depends on**: BOOT-06

- [ ] **INFRA-17** Close the remaining plaintext durable-secret gaps
  - **Files**: `packages/database/src/database/tables/compute.py:463`;
    `packages/shared/src/shared/aws_connections.py:156-161`;
    `packages/execution/src/execution/secrets/crypto.py:34-92`
  - **Change**: the AWS connection `external_id` is stored plaintext in a
    dedicated column (`tables/compute.py:463`) and again inside the JSON payload,
    with only `repr=False` protecting it
    (`shared/aws_connections.py:156-161`). The repo already has one encryption
    owner — `WorkspaceSecretCipher` (AES-GCM, HKDF from the workspace signing
    key, AAD-bound, versioned `wssec:v1:` prefix,
    `execution/secrets/crypto.py:14-92`) — and `gateway` already depends on
    `execution`. Either encrypt `external_id` through that owner or record
    explicitly, in `packages/compute/AGENTS.md`, that it is a confused-deputy
    nonce rather than an authenticator and is deliberately stored in the clear.
    Do **not** leave it undecided. Note the honest limit: the cipher's root is
    `workspaces.signing_key`, itself a plaintext column
    (`packages/database/src/database/tables/identity.py:80`), so this is
    domain-separated at-rest encryption within one database, not envelope
    encryption — say so wherever the decision is recorded.
  - **Risk**: encrypting a column read on the connection hot path adds a KDF per
    read; cache the derived key per workspace if it shows up. Changing the column
    is a baseline rewrite, which is ordinary predeployment work.
  - **Acceptance**: whichever branch is chosen, `SELECT external_id FROM aws_account_connections`
    returns either ciphertext with the `wssec:v1:` prefix or values the
    `AGENTS.md` decision explicitly sanctions — and a live AWS connection still
    reaches `AwsAccountAuthorizationPhase.Ready` afterwards.
  - **Depends on**: none

- [ ] **INFRA-18** Remove the prior architecture's credential backups from the working tree
  - **Files**: `infrastructure/secrets-backup/lazycloud-prod-secrets.json`,
    `infrastructure/secrets-backup/lazycloud-shared-secrets.json`,
    `infrastructure/secrets-backup/lazycloud-staging-secrets.json`;
    `infrastructure/terraform/kubeconfig`, `infrastructure/terraform/talosconfig`
  - **Change**: these files hold live third-party credentials for the superseded
    EKS/Talos/Cloudflare architecture — AWS access keys, WorkOS, Polar, Resend,
    Cloudflare, and Prefect credentials among them. They are correctly untracked
    (`git ls-files infrastructure/` returns nothing;
    `.gitignore` covers `secrets-backup/`, `kubeconfig`, `talosconfig`,
    `*.tfstate`), so nothing leaked into history — but they sit unencrypted in
    the working tree of an actively agent-driven repository. **Owner action, not
    an automated one**: rotate every credential that is still live, then delete
    the directory. Also settle the orphaned `lazycloud-shared` CloudFormation
    stack (verified live 2026-07-31: an EKS/Karpenter substrate created
    2025-12-20 with ECR repos `backend-api`, `backend-background-workers`,
    `backend-crons`, `web`, a `lazycloud/shared-secrets` Secrets Manager secret,
    and Karpenter/EBS-CSI/EFS-CSI/ExternalSecrets roles) — it belongs to no
    source in this repo and is either cost with no owner or a dependency nobody
    has written down.
  - **Risk**: deleting is irreversible and some credential may still be in use by
    something outside this repo. Rotate first, delete second. Do not delete the
    `lazycloud-shared` stack without confirming what still consumes it — the
    account also carries unrelated EKS, SageMaker and DataZone stacks.
  - **Acceptance**: `ls infrastructure/secrets-backup` reports no such directory;
    each rotated credential is confirmed rejected at its provider. A written
    decision on `lazycloud-shared` (keep, with an owner and a source; or delete,
    with the resource list confirmed) recorded on the owning ticket.
  - **Depends on**: none

- [ ] **INFRA-19** Write the production runbook
  - **Files**: new file: `deploy/RUNBOOK.md`; `deploy/AGENTS.md`
  - **Change**: one document covering exactly what an operator cannot derive from
    the code: bring-up order and the two sidecar recreate hazards
    (`control-plane` recreate destroys both `tailnet-gateway` and
    `public-ingress`); how to read a failed node (durable event → excerpt →
    `ec2:GetConsoleOutput` → SSM `AWS-RunShellScript`, in that order, with the
    exact commands); how to reach Prometheus and Alertmanager without a public
    port; what each alert means and its first diagnostic step; secret inventory
    and rotation procedure for the tunnel credentials, the Tailscale OAuth
    client, and the admin scrape token; and the one irreversible action list
    (launch-template version deletion, tailnet device deletion — which
    invalidates the sidecar identity per `deploy/AGENTS.md:50-53`, customer stack
    deletion). No architecture narrative; a reader with a page in hand.
  - **Risk**: a runbook that drifts is worse than none. Keep every command
    copy-pasteable and verified at the time of writing, and reference file paths
    rather than restating logic.
  - **Acceptance**: an operator who has not read this plan follows the runbook
    from a cold stack to a managed node at `ready: 1`, then diagnoses one
    deliberately broken node to its root cause, using only the runbook.
  - **Depends on**: INFRA-06, INFRA-09, INFRA-13, INFRA-15

## Deletions

| File or symbol | Why safe to delete | What replaces it |
| --- | --- | --- |
| `deploy/tailnet-gateway/serve.json` (whole file, and the directory) | Its only consumer is `TS_SERVE_CONFIG` at `compose.yaml:561`. The `AllowFunnel` block (`:16-18`) has never functioned — `deploy/tailnet/policy.json.tftpl` grants no `funnel` nodeAttr. The `TCP 443` / `Web` handlers proxy to `127.0.0.1:9000`, which no consumer uses: agents and workers dial port 9000 directly (`policy.json.tftpl:16, 20`; `compose.yaml:24`). | The `public-ingress` cloudflared sidecar (INFRA-06) |
| `TS_SERVE_CONFIG` env and the `./deploy/tailnet-gateway:/config:ro` mount | `compose.yaml:561, 571` — sole references to the deleted file | — |
| `deploy/connected-aws/operator-ssm-diagnostics-policy.json` | Referenced by nothing (`grep -rn operator-ssm-diagnostics-policy .` finds only the file itself). Its content is correct and is being inlined. | The `OperatorRole` statements added by INFRA-09 in `control-stack.yaml` |
| The live `describe_internal_pool` call in `_verify_active_node` | `provider_enrollment.py:195-198` — an unauthenticated request must not drive an `sts:AssumeRole` plus `DescribeAutoScalingGroups` into a customer account | The durable provider-instance inventory the reconciler maintains (INFRA-03) |
| The `_count`/`_min`/`_max`/`_last` histogram rendering | `metrics.py:72-79` — not Prometheus histogram buckets; `rate()` and `histogram_quantile()` over them are wrong | Correctly typed summary output (INFRA-12) |
| `infrastructure/secrets-backup/` and `infrastructure/terraform/{kubeconfig,talosconfig}` | Untracked artefacts of the superseded EKS/Talos architecture holding live third-party credentials; no source in this repo consumes them | Nothing — rotate first, then delete (INFRA-18) |

## Track acceptance

1. From an EC2 instance in the test customer account with **no tailnet session**,
   `curl -fsS https://<public hostname>/health` returns the health JSON over a
   verified TLS chain, and an SSE stream survives past 100 s.
2. Against that public origin, 200 concurrent
   `POST /gateway/provider-nodes/enroll` with unresolvable proof URLs leave
   `GET /health` answering under 200 ms throughout; excess requests return `429`
   or `503` immediately; at most 8 outbound STS connections exist at any instant;
   and CloudTrail shows **zero** `DescribeAutoScalingGroups` calls attributable
   to those requests.
3. `python -m tests.e2e.external.aws.one_machine_readiness` reaches `ready: 1`
   with the node enrolling over the public origin.
4. A deliberately broken node (bad agent artifact URL) is diagnosed to root cause
   from the durable record alone — event row plus diagnostic excerpt — and the
   same cause is independently confirmed via `ec2:GetConsoleOutput` and an SSM
   `AWS-RunShellScript` invocation, with no SSH.
5. `promtool check metrics` and `promtool check rules` both exit 0; stopping the
   control plane produces a `ControlPlaneDown` notification at the configured
   receiver; each shipped alert rule has been observed firing at least once.
6. `aws ec2 describe-launch-template-versions` for the test connection returns
   one version, and its decoded user-data contains no `tskey-`.
7. `docker compose up -d` from a clean checkout brings the stack, both sidecars,
   Prometheus and Alertmanager to healthy, and `grep -rn "serve.json\|AllowFunnel" .`
   returns nothing outside `.git`.

## Open questions for the owner

1. **Ingress choice — decide before INFRA-06 starts.** The plan assumes a
   Cloudflare Tunnel because the evidence says you already own `lazycloud.dev`
   and a Cloudflare zone (`APP_URL=https://lazycloud.dev`, plus
   `CLOUDFLARE_ZONE_ID`/`CLOUDFLARE_API_KEY`/`CLOUDFLARE_ACCOUNT_ID`/
   `CLOUDFLARE_TUNNEL_TOKEN` in the untracked `infrastructure/secrets-backup/`
   from the prior architecture). If that account is gone or you reject a CDN
   dependency, the substitute is Caddy with Let's Encrypt on a host with a public
   IP and 443 open — which changes only INFRA-06 and the header name in INFRA-02,
   but gives up DDoS absorption in front of the enrolment endpoint. An ALB is
   **not** a like-for-like substitute: the control plane does not run in AWS, and
   moving it there is a separate multi-week project.
2. **The `lazycloud-shared` CloudFormation stack.** Verified present in account
   534742592531, created 2025-12-20, defining an EKS/Karpenter substrate, four
   ECR repositories, and a Secrets Manager secret — with no source anywhere in
   this repo. Keep it (and record an owner and a source), or delete it? It is
   accruing cost with no plan attached.
3. **Alert delivery.** Where should Alertmanager send — email, a Slack webhook,
   PagerDuty? There is no alerting integration of any kind in the repo today, so
   this is a first choice, not a change.
4. **Customer re-authorization for INFRA-16.** Pruning stale launch-template
   versions needs `ec2:DeleteLaunchTemplateVersions` in the customer connection
   role, which means a new authorization generation and every connected customer
   re-running CloudFormation. Do that now while the only connection is the test
   one, or accept that superseded keys sit readable in customer accounts until
   each pool's template is deleted?
5. **Blast radius of `ssm:SendCommand` (INFRA-09).** It is remote code execution
   on managed nodes, tag-scoped to `cloud-pool:managed-by=control-plane`. Confirm
   that the acceptance operator role is an acceptable holder of that capability,
   or restrict it to a separate break-glass role assumed only for diagnosis.
6. **`external_id` (INFRA-17).** Encrypt it, or record in `AGENTS.md` that it is
   deliberately clear because it is a confused-deputy nonce? Either is
   defensible; leaving it unstated is not.
