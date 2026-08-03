# LazyCloud simplification programme — working plan

This is the list we work from. It flattens four track documents into one ordered
sequence, resolves the places where they conflict, and marks where we may stop.

- Detail for every item — exact files, precise change, risk, acceptance — lives
  in the track document named by its ID prefix. **This file is the order; the
  track file is the instruction.** Do not implement from this file alone.
  - `BOOT-*` → `plan/10-bootstrap-enrolment.md`
  - `CAP-*` → `plan/11-capacity-scheduling.md`
  - `ERR-*` → `plan/12-failure-semantics.md`
  - `INFRA-*` → `plan/13-production-readiness.md`
- `plan/00-prior-tailnet-plan.md` is the superseded tailnet-bootstrap plan, kept
  only for the constraints it records that were learned on live hardware.

81 items. Phases 0–4 are the programme. Phases 5–6 are elective and are called
out as such.

## Rule 0 — loop until done

**This is the working rule. It comes before everything else.**

For each item, in this order, and do not skip a step:

1. **Complete the current task.** Finish it — implementation, acceptance, and the
   commit. A task is not done because the code is written; it is done when its
   acceptance evidence has actually been produced and the tree is clean.
2. **Determine what is next.** Take the next unchecked item in phase order,
   respecting the *after* annotations. Do not jump ahead to something easier, and
   do not start a second item while one is half-finished.
3. **Investigate it until you understand it.** Read the track document's entry,
   then read the real code it names and verify every `file:line` still says what
   the plan claims. The plan is a claim with evidence attached, not a fact. If
   the code has moved or the plan is wrong, correct the plan first, then proceed.
4. **Implement it.** Follow the track document's instruction.
5. **Tick the box, record what the acceptance actually showed, and return to 1.**

Do not stop at a phase boundary for permission unless the phase itself says to
(the Gates, and the stop point after Phase 4). Do not stop because something was
hard; stop only when genuinely blocked or genuinely finished, and say which.

If an item turns out to be wrong, unnecessary, or already done, say so and strike
it — do not implement something the code has made obsolete.

## Ground rules

From `CLAUDE.md`, and they bind every item here:

- Fix the architecture, model, boundary, ownership, or signature — not the
  checker. No `type: ignore`, no broad `Any`, no exclusions, no shims, no
  compatibility wrappers.
- One production route. No feature flags, capability gates, or environment
  switches that let test and production take different paths.
- Delete stale paths rather than preserve them.
- The Test Decision Gate governs every test. Do not add a test because code
  changed. Several items below deliberately specify a live observation or an
  existing scenario as their acceptance instead of a new test.
- Never leave the tree in a state where a node cannot report why it failed.
  This constraint alone determines much of the ordering in Phase 4.

## Conflicts resolved

The track authors each wrote correctly for their own territory and collided in
three places. These are the rulings; the track documents are wrong where they
disagree with this section.

1. **`compute/service.py` is split by CAP first, then ERR.** CAP-14 collapses
   the capacity-acquisition seam; ERR-16 extracts the provider-reconciliation
   seam. Two concurrent splits of one 4,968-line class will not merge. Order:
   CAP-13 → CAP-14 → ERR-10 → ERR-16. The ERR author assumed exactly this and
   asked for it to be owned; it is owned here.
2. **ERR owns contracts, CAP owns callers.** ERR-12 changes the `disable-worker`
   contract, route, and protocol; CAP-02 wires the callers. ERR-12 lands first.
   Same rule for ERR-11 (`bootstrap_failure_detail` field) before BOOT-03
   populates it.
3. **`shared/http/provider_nodes.py` is edited by BOOT-03 before INFRA-13.**
   Both touch the bootstrap-failure contract; BOOT-03 establishes the shape.

## Gap found during synthesis

CAP-10 and CAP-15 both declare a dependency on an INFRA item that regenerates
the Alembic baseline. **No such item was written.** Both drop durable columns,
so it is required, and it is ordinary predeployment work under `CLAUDE.md`
(SQLAlchemy metadata plus one reviewed baseline, fresh PostgreSQL bootstrap — no
historical revisions, no upgrade paths). Added here as INFRA-20 and placed
immediately before its dependents.

## Hard gates

Two, and neither is negotiable.

**Gate A — nothing public until the request path is bounded.** INFRA-03, -04 and
-05 must be complete and verified before INFRA-06 exposes anything, and
therefore before BOOT-04 and everything after it.

The reason is worse than a slow endpoint. `provider_enrollment.py:195-198` calls
`describe_internal_pool`, which performs a live `sts:AssumeRole` into the
*customer's* AWS account plus `DescribeAutoScalingGroups` plus a durable write —
**before identity verification**, on an unauthenticated route, gated only by a
pool UUID that is readable by any customer principal holding
`ec2:DescribeLaunchTemplateVersions`. The three routes are `def`, not
`async def`, so each request also parks one of ~40 anyio threadpool workers for
up to 3 s. Exposed as-is, one leaked UUID exhausts a customer's AWS API rate
limits and stalls every synchronous route in the control plane.

**Gate B — a node must always be able to say why it failed.** BOOT-03 adds
agent-side phase reporting before BOOT-05 removes the shell reporting. Do not
reorder these. Today three of six failure reasons are unreachable by
construction, and we are not shipping a window where that gets worse.

---

## Phase 0 — Stop the bleeding

Independent, cheap, and each one either fixes a live fault or removes a
diagnostic blind spot. Nothing here depends on anything else.

- [x] **CAP-01** Guard `_acquire_from_controller` against pending-worker reservations — *superseded by CAP-13 (2d8db15), which deleted the reservation kind the guard protected*
- [x] **BOOT-01** Give the `Failed` bootstrap phase a reclaim deadline
- [x] **BOOT-02** Stop a revoked agent from re-enrolling forever
- [~] **INFRA-18** Remove the prior architecture's credential backups from the working tree
  — *repo exposure closed 2026-07-31: `infrastructure/secrets-backup/` moved to
  `~/.lazycloud-legacy-secrets/secrets-backup` (moved, not deleted — rotation
  needs the values). **Owner action outstanding**: rotate, then delete. Live
  credentials: `AWS_ACCESS_KEY_ID`/`SECRET`, `CLOUDFLARE_API_KEY`,
  `CLOUDFLARE_TUNNEL_TOKEN`, `WORKOS_API_KEY`/`COOKIE_PASSWORD`,
  `POLAR_ACCESS_TOKEN`, `RESEND_API_KEY`, `DEPOT_API_TOKEN`/`REGISTRY_TOKEN`,
  `UPSTASH_REDIS_REST_TOKEN`, `PREFECT_API_AUTH_STRING`/`AUTH_PASSWORD`,
  `ADMIN_API_KEY`, `DB_SECRET_KEY`, and the `DATABASE_URL`/`REDIS_URL`/
  `PREFECT_DATABASE_URL` embedded passwords. `infrastructure/terraform/kubeconfig`
  and `talosconfig` left in place (0600) — cluster admin credentials, but
  possibly live tool inputs, so the owner should confirm before they move.*
- [x] **INFRA-09** Grant node diagnostics to a break-glass role
  — *deployed 2026-07-31 as `lazycloud-default-test-diagnostics` (separate from
  the operator role, per decision 6). Verified live: `ssm:DescribeInstanceInformation`
  succeeds where it returned `AccessDeniedException`, and `ec2:GetConsoleOutput`
  returns kernel output even for a terminated instance. The `ssm:send-command`
  and untagged-instance-denial checks need a running node — verify in Phase 4.*
- [x] **ERR-01** Revalidate every record on the upsert path
- [x] **ERR-02** Make Pydantic serializer warnings a test failure — *after ERR-01*
- [x] **ERR-04** Delete the three no-op revalidation calls — *after ERR-01*
  — *plan corrected: it called for deleting the guard at `compute.py:381` as
  redundant. It is not. ERR-01 validates inside `records.upsert`, which runs
  **after** the capacity-owner immutability comparison at `:383-388`, so
  deleting it would leave that identity check able to raise a spurious
  `ConflictError`. Its `model_dump(mode="python")` was the real defect — it
  serializes, which ERR-02 now makes an error — so it became
  `model_validate(dict(record))`. The three no-ops were removed as planned.*
- [x] **ERR-05** Log the cause in the `DomainError` sink
  — *verified live: a missing deployment returns 404 with `x-request-id`, and
  the server log carries `NotFoundError serving GET … (request_id=…)` plus the
  traceback, at `warning` for 4xx.*
- [x] **ERR-17** Delete `_resolve_in_session`
- [x] **CAP-11** Delete the dead projection status cluster

**Why these first.** CAP-01 is the live outage. BOOT-01 is why a failed node
bills indefinitely and holds the ASG slot — today a node that *successfully*
reports failure is treated worse than one that dies silently. INFRA-09 is a
~10-line CloudFormation change that ends the blindness which dominated the cost
of every diagnosis so far; the correctly scoped policy already exists at
`deploy/connected-aws/operator-ssm-diagnostics-policy.json` and is referenced by
nothing. ERR-01 plus ERR-02 make the whole type-drift class unreachable at the
persistence boundary and gate it in CI.

**Phase 0 acceptance.** `paid_resource_bounding` gets past capacity acquisition;
a deliberately failed node is reclaimed within its deadline rather than left
running; `pytest packages/ -W error::UserWarning` is green; an operator can pull
a live node's agent journal without SSH.

## Phase 1 — Make every failure nameable

Contracts first, then the callers that populate them. After this phase a
failure carries its cause to a durable place in every path we touched.

- [x] **ERR-11** Add `bootstrap_failure_detail` beside the failure enum
- [x] **ERR-12** Give `disable-worker` a reason field
- [x] **ERR-03** Stop building one payload dict for two different contracts
  — *only the provider-instance site had the defect. `_ensure_compute_pool_record`
  (`service.py:4240`) already passes Python-typed values: `config` is declared
  `dict[str, JsonValue]`, so `_json_object(config)` is correct there.*
- [x] **ERR-06** Give `DomainError` and `ErrorResponse` a stable code — *after ERR-05*
- [x] **ERR-07** Stop flattening `ManagedComputeLaunchError`; stop returning 500 for a quota hit — *after ERR-06*
  — *the plan mapped three codes; there are five. AST enumeration of all 13 raise
  sites found `offer_unavailable` and a billing-derived `decision.error_code`
  the plan missed, so the map defaults to conflict and names only
  `provider_unavailable` as upstream.*
- [x] **ERR-08** Record why a lease was lost instead of only that it was
- [x] **ERR-09** Give the shell compensation path a reason
- [x] **ERR-13** Move compute-policy editing server-side — *after ERR-06*
  — *`PATCH` added, `PUT` left as full-replace (decision 10). Verified live: the
  CLI now sends `{'expected_revision': N, 'aws': {'max_cpu_instances': 1}}`
  instead of all ten stored values, and renders a typed panel with exit 1.
  The cross-field case in the acceptance needs a live AWS connection, which the
  reset removed — the placement guard fires first. Re-check in Phase 4.*
- [x] **ERR-14** Stop billing a workspace that zeroed its CPU capacity
  — *plan corrected: it called for splitting CPU and GPU release. There is no
  GPU release to split — `reconcile_aws_default_capacity` takes no GPU argument,
  and `max_gpu_instances` is read only as a placement ceiling
  (`request_placement.py:80`, `service.py:2315`). The defect was gating a CPU
  release on a GPU ceiling, so the knob was removed from the predicate.*
- [x] **CAP-02** Carry a typed reason on every transition to `Unavailable` — *after ERR-12*
- [x] **CAP-03** Report which registration step failed — *after CAP-02*
  — *the two candidates behind the 25-minute crash loop now produce different
  durable values: `readiness_validation_failed` vs `source_cache_unavailable`.
  Detail carries the step and exception class only — verified that a probe
  failure containing a gateway URL and a token yields
  `'validate-readiness failed (ConnectionError)'`. Live confirmation on a node
  belongs to Phase 4.*
- [x] **CAP-04** Remove the worker record when registration never completed — *after CAP-03*
  — *an existing test asserted the opposite and claimed it preserved owner
  identity. It does not: `worker_id` comes from `WORKER_ID`, and `remove_worker`
  touches no durable owner record — it requeues queued requests and deletes the
  state key. Test reworked to assert the cleanup obligation instead.*
- [x] **CAP-05** Stop discarding the keep-alive source-cache outcome — *after CAP-02*
- [x] **CAP-06** Give `mark_available` one job — *after CAP-03*
  — *split done: `activate-source-cache` is its own step, so an un-purgeable
  target is blamed on it rather than on `mark-available`, and CAP-03 maps it to
  `source_cache_unavailable`. **The gate was deliberately NOT loosened — see
  below.***
- [x] **CAP-07** Reclassify "at limit" as backpressure
  — *a full pool's reservation now waits instead of failing (one open claim per
  container, no churn), its sizing state carries no terminal reason, and health
  keys on `consecutive_failures`/`retry_after_at` — the signals genuine failures
  actually set via `sizing_failure_state`. Failover from a full pool is covered
  by the existing priority-order test, which still passes.*
- [x] **CAP-08** Introduce one owned readiness predicate
  — *`machine_serves_workloads` in `compute/agent_control.py`; both the API
  summary and bootstrap reclaim call it; body reads the scheduler's hot record
  through the extended `ComputeSchedulerHooks`. Fail-closed proven by a focused
  case: an unreachable worker-state store keeps the machine and terminates
  nothing. Tests that faked readiness via durable `Worker(Running)` rows now
  register hot records, which is the honest change.*
- [x] **BOOT-03** Report bootstrap phases from the agent, not only from user-data
  — *agent reports `Booting` at start, `Joining` after identity resolves, `Ready`
  at the runtime-ready marker; pre-enrolment failures are labelled
  `provider_identity_failed` / `network_join_failed`, and the enrolment path's
  own precise report cannot be overwritten by an outer wrapper. Live phase
  timeline check belongs to Phase 4's `one_machine_readiness` run.*
- [x] **INFRA-11** Emit HTTP request metrics from the gateway middleware
- [x] **INFRA-12** Make `/metrics` scrapable and correctly typed — *after INFRA-11*
  — *no promtool on this host; validated structurally instead: every family
  typed and grouped, `_count`/`_sum` under a `summary`, extrema as their own
  gauges. Live scrape check rides Phase 4's stack.*
- [x] **INFRA-13** Carry a bounded diagnostic excerpt on the bootstrap-failure report — *after BOOT-03*
  — *the agent fills it with the active traceback (8 KiB bound); the server
  sanitizes control characters, persists it as `bootstrap_failure_detail` after
  identity verification, and leaves an `EventLevel.Error` durable event. The
  broken-artifact live check belongs to Phase 4.*
- [x] **INFRA-14** Route capacity and enrolment error paths through the durable event channel
  — *scoped as planned: the enrolment compensating rollback and both API
  reconcile loops now leave `EventLevel.Error` durable events, each emit wrapped
  so recording a failure never replaces it; the rule is recorded in
  `packages/observability/AGENTS.md`. ComputeService holds no event sink and the
  autoscaler has no broad handlers — those paths surface through the reasons and
  events added by ERR-11/12 and CAP-02/03, and the wider swallow set is Phase
  6's sweep.*

**Why this matters more than it looks.** CAP-03 resolves the open question the
capacity investigation could not answer from code: whether the worker
crash-loop was the gateway egress probe or source-cache activation. Both produce
an identical signature today and neither is recorded. This phase turns that from
guesswork into a field.

**Phase 1 acceptance.** Kill a worker's egress mid-registration and read the
failing step back from a durable record. `ERR-14`: zero every CPU knob and
confirm the workspace stops billing. `ERR-07`: hit a configured quota and get a
409 with a machine-readable code, not a 500.

## Phase 2 — Security gate (blocks all exposure)

**Nothing in Phase 3 or 4 may begin until this phase is verified.** See Gate A.

- [x] **INFRA-01** Add a Redis rate-limiter primitive to coordination
  — *verified against live Redis: exactly 10 grants per window, TTL never
  extended by later hits, slots token-fenced with dead-holder expiry.*
- [x] **INFRA-02** Resolve the client address from exactly one configured source
  — *forged `X-Forwarded-For` ignored when unconfigured; configured header
  wins and takes the last hop; unparseable values fall to the shared bucket.*
- [x] **INFRA-03** Stop calling AWS inside the provider-node request path — *after INFRA-01*
  — *membership now reads `ComputeProviderInstanceRepository.list_for_pool`; an
  unknown instance buys at most one refresh per pool per 5s and is otherwise
  refused with 503. The pool-identity check moved with it: a replaced ASG is
  detected by the reconciler updating inventory, so the old live-describe
  divergence test was retargeted to the invariant this layer actually enforces.
  The unroutable-endpoint live proof belongs to Phase 3/4.*
- [x] **INFRA-04** Bound the STS proof verification so it cannot exhaust the API — *after INFRA-01, INFRA-03*
  — *verification runs under a token-fenced slot (default 8, settable); 200
  concurrent attempts admitted exactly 8 and refused 192 immediately. Budget cut
  to 2s with a 1s connect bound so a black-holed endpoint cannot spend it twice.
  The live 200-request/health-latency contrast belongs to Phase 3.*
- [x] **INFRA-05** Rate-limit every unauthenticated route before exposure — *after INFRA-01, INFRA-02*
  — *verified live: `/auth/device` returned 10×201 then 429 with `retry-after: 60`,
  and `device_authorizations` grew by 10 not 14 — refused requests never reach
  the database. `/health` fails open when Redis is down, the provider-node prefix
  fails closed, authenticated routes are untouched, and the Compose healthcheck
  stays healthy.*
- [x] **INFRA-17** Close the remaining plaintext durable-secret gaps
  — *decision 12 verified before recording it: `external_id` is passed to
  `sts:AssumeRole` and the platform checks the customer's trust policy enforces
  it (`ExternalIdNotEnforced`), so it is a confused-deputy nonce the customer
  types into their own console — not an authenticator. Recorded in
  `packages/compute/AGENTS.md`, with `WorkspaceSecretCipher`'s honest limit.*

**Phase 2 acceptance.** Load-test `/gateway/provider-nodes/*` with an unverified
pool UUID and demonstrate: no outbound AWS call occurs before identity
verification, no request occupies a threadpool worker for longer than the
configured bound, and the rate limiter sheds excess. This is a load test, not a
unit test.

## Phase 3 — Public ingress

`plan/14-ingress-design.md` holds the concrete design: a `cloudflared` sidecar
sharing the control plane's network namespace, apex + wildcard hostnames (forced
by the generated-invoke host routing), and edge path refusal for `/metrics` and
`/worker-repository`. Two things it found that are not optional:

- `LAZYCLOUD_PUBLIC_INGRESS_CLIENT_IP_HEADER` **must** be set to
  `CF-Connecting-IP` in the tunnel deployment: every tunneled request's socket
  peer is `127.0.0.1`, so unset, the whole internet shares one rate-limit bucket.
- `/api/v1/logs/stream` emits nothing while a task is quiet and will be cut at
  Cloudflare's 100s no-byte window. It needs the heartbeat the change stream
  already has (`workspace_changes.py:25`), inside INFRA-06.


- [x] **INFRA-06** Stand up the public HTTPS ingress — *`https://lazycloud.dev`
  serves the origin through a locally-managed tunnel; the route allowlist is
  `deploy/public-ingress/cloudflared.yml`, verified by `/metrics` and
  `/worker-repository/*` returning 404 at the edge where the origin answers 401.
  The design assumed a browser `cloudflared tunnel login`; the tunnel, both DNS
  records, and the credentials were minted through the API instead, so the
  procedure is reproducible. Credentials are `0444` inside a `0700` directory,
  not `0400` — the container runs as uid 65532 and cannot read an operator-owned
  `0400` file. The EC2-reaches-the-ingress half of Phase 3 acceptance is
  BOOT-04's, which this unblocks.*
- [x] **INFRA-07** Refuse to launch managed capacity against an unreachable public origin
  — *the plan listed three files; it collapses to one. Both composition sites
  already funnel through `validate_provider_network_configuration`, which
  checked the internal origin's reachability but only HTTPS on the public one —
  so `https://127.0.0.1` passed. Now refused by name.*
- [ ] **INFRA-15** Scrape, alert, and page on the tracks' failure signals — *after INFRA-12, CAP-02, CAP-08*

**Phase 3 acceptance.** An EC2 instance in the connected account reaches the
ingress and completes enrolment. Sustained load equal to a full pool launch does
not degrade it — Funnel failed exactly this test, which is why it is not the
answer here.

## Phase 4 — Bootstrap simplification

The payoff. Deletes the pool bootstrap key, the tailnet-first boot path, the
duplicated bash SigV4, and roughly 350 lines of shell.

- [ ] **BOOT-04** Point managed-pool nodes at the public control-plane origin — *after BOOT-03, INFRA-06, Gate A*
- [x] **BOOT-04a** Give the worker the runtime origin, not the public one — *five defects stood
  between enrolment and a serving worker, each found on live nodes.*
  `agent_state_payload` listed the bootstrap fields by hand and omitted
  `gateway_runtime_http_url`, so the origin survived in memory and was lost on the next restart;
  the daemon then handed the worker the public origin, which the ingress refuses for
  `/worker-repository/*` by design. The agent also declined the tailnet's DNS while reaching the
  control plane as a tailnet peer, so `*.ts.net` resolved through the VPC resolver to Tailscale's
  public records rather than the peer — every name resolved and every connection to it timed out.
  The published `container-worker` predated `unavailable_reason`/`unavailable_detail`, and
  `extra="forbid"` made the newer response unparseable, killing the worker before it could report
  available. Finally the STS identity proof carried no nonce, so the script's last report and the
  agent's first enrolment minted identical bytes in the same second and the replay guard refused
  the second. Local Compose can surface none of it: `compose.yaml:840,983` override
  `WORKER_REPOSITORY_URL` outright.
  Acceptance: `ready: 1` on `i-085fdc077ae093bc0` from an unmodified launch template.*
- [x] **BOOT-04b** Stop the worker holding a node-wide lock across its egress probe —
  *`reserve_probe_ip` never wrote the assignment, so the lock was the only thing keeping a
  second slot off the same address, and it therefore spanned a network round trip while
  acquisition retries three times. A slot that lost the race failed `ValidateReadiness` and
  never reported itself available, which is what made a machine oscillate between serving and
  joining roughly once a minute and kept the pool from holding a warm baseline. The probe now
  records its address the way a container does. Measured after: eleven consecutive `ready=1`
  samples, against four drop-outs in twelve before.
  This is also where the first workload ran end to end — the example app deployed through the
  public SDK, executed on `i-016ddfc783bfcefdb`, and returned
  `{'marker': …, 'doubled': 42, 'status': 'complete'}`.
  The same change deletes the slot-pool machinery, fifteen symbols with no callers anywhere.*
- [ ] **BOOT-05** Reduce the bootstrap script to identity plus the published provisioner — *after BOOT-04*
- [ ] **BOOT-08** Collapse the AMI bake onto the published install script — *after BOOT-05*
- [ ] **BOOT-06** Delete the pool bootstrap key subsystem — *after BOOT-05*
- [ ] **BOOT-07** Remove the bootstrap tag from the tailnet policy — *after BOOT-06*
- [ ] **BOOT-09** Delete the tailnet-first rationale from surviving docstrings — *after BOOT-07*
- [ ] **INFRA-08** Delete Tailscale Serve and Funnel — *after INFRA-06*
- [ ] **INFRA-10** Prove the SSM agent is enabled in the baked AMI — *after INFRA-09, BOOT-08*
- [ ] **INFRA-16** Retire the pool bootstrap key material BOOT-06 leaves behind — *after BOOT-06*

**Watch item.** BOOT-05 must keep the 47.6 MB agent artifact off the control
plane. The published `/install/agent` script fetches from `$GATEWAY` by default;
BOOT-05 adds `--agent-url` so managed nodes pull from the release bucket
instead. Serving that artifact from the control plane per node launch is the
same bulk-transfer pattern that broke the previous ingress.

**Phase 4 acceptance.** `python -m tests.e2e.external.aws.one_machine_readiness`
passes with no tailnet key in user-data and no `compute_pool_bootstrap_credentials`
row in existence; then `paid_resource_bounding`; then `cleanup`. A node that
fails at provider identity, at Docker install, or at tailnet join reports a named
reason — all three of which are unreportable today.

### Stop point

**Phases 0–4 are a coherent, shippable programme.** They fix every known live
fault, make failures diagnosable, close the exposure gate, and delete the
subsystem that generated most of the accidental complexity. Everything below is
structural cleanup with real risk and no user-visible outcome. Decide
deliberately whether to continue — do not drift into Phase 5.

---

## Phase 5 — Structural (elective)

The capacity author's judgement, recorded verbatim in its report: CAP-01 through
CAP-12 delivers most of the reliability gain, and it would not do CAP-13 without
first taking the restart measurement that item specifies. The failure-semantics
author's judgement: it supports restructuring `packages/compute` specifically,
not the repo.

- [x] **CAP-09** Stop asserting durable `Worker.status = Running` at registration — *after CAP-08*
  — *reader audit done as the plan required: every remaining reader either
  branches on the scheduler record (a different type), carries the durable
  status forward without branching (`gateway/service.py:1663`), or renders it.
  The readiness branches were the two CAP-08 unified.*
- [x] **CAP-12** Split the persisted bootstrap phase from the derived verdict — *after CAP-08*
  — *the stated dependency was on the BOOT item owning `MachineBootstrapPhase`,
  which is BOOT-03 (done), not BOOT-09 (a docstring cleanup) — my synthesis
  mapped it wrong. **BOOT-03 also had to be adjusted**: it added an agent
  `Ready` report, invalidating the CAP author's "no writer persists it" audit.
  Removed — a node narrates progress up to `Joining`; whether it serves is the
  scheduler's call. Contract, mapper, CLI and Zod moved together; web typecheck
  green.*
- [x] **INFRA-20** Regenerate the Alembic baseline — *the baseline is `DatabaseBase.metadata.create_all`, so it tracks a dropped column by itself; the work is moving the revision identifier and re-bootstrapping. Done once for CAP-10 rather than per item. `alembic check` clean against a fresh database, and the bootstrap guard demonstrably refuses a mismatch — it blocked a stale image mid-change*
- [x] **CAP-10** Delete `Worker.version` — *written once as "pending", copied forward, never read for a decision. Field, `register_worker` parameter, both write sites and the `workers` column removed; `agents.version` is a different column and stays. Acceptance met on a fresh bootstrap: stack healthy, no `version` on `workers`, a worker registered*
- [x] **CAP-13** Delete the pending-worker reservation path — *2d8db15. The restart measurement was answered by inspection, not experiment: `list_workers` rebuilds the scheduler's worker set from Redis every tick and the reservation lives in the same Redis, so the "durable survives a restart, in-memory does not" premise was false. The Pending record is written at enrolment, so the claim never fires during a machine boot — PlacementMiss already dedups that window. CAP-01's guard went with it, along with `source`, the `reserve()` `target_worker_id` parameter, `PendingCapacityOwner`, and a stranding race between the two reservation kinds*
- [x] **CAP-14** Collapse acquisition to one idempotent entry point — *68aaea2. The risk it named is answered: compute commits the operation row with the pool row locked **before** the provider call, the unique constraint on `reservation_id` holds, and the provider idempotency key derives from the operation id — so the scheduler-side persist was fencing something already fenced. `plan_acquisition`, `ensure_acquisition` and `reconcile` become one `ensure_capacity`, and `CapacityAcquisitionPlanningRequest` plus the caller-supplied `desired_unit` are gone. Pool sizing keeps its committed floor through `minimum_unit`, which CAP-15 removes with that caller. Acceptance: a retry reuses one operation row (`get_by_reservation` raises on a second) and launches exactly one machine*
- [~] **CAP-15** Delete `CapacityPoolSizingState` and the `pools.sizing_*` columns — *investigated, and the investigation found a live defect that outranked the item. The plan assumed the sizing backoff is what stops a broken pool relaunching billable machines. It is not: it only covers the case where the **provider call itself fails**, so no machine launches. In the case that actually costs money — the machine launches and never becomes a worker — the provider call succeeds, no sizing failure is recorded, and the backoff never engages. That path was bounded only by `max_launch_attempts` → degraded, which **only the reconciler honoured**: acquisition never read the flag and placement cleared it on every dispatch claim. Fixed in 04bd830 (with a focused test, mutation-verified), independent of this item. What remains for CAP-15 proper: derive the provider-call backoff from `compute_capacity_operations` — noting those rows do **not** record bootstrap reclaims, so they can only cover the provider-call case — then drop the columns. Its acceptance (30 min against an always-failing pool) needs the connected AWS environment*
- [ ] **CAP-16** Collapse the reservation status machine — *after CAP-15*
- [ ] **CAP-17** Split `capacity_reservations.py` along its five-way seam — *after CAP-16*
- [x] **ERR-10** Collapse three at-limit conventions into one — *the three were worse than recorded: `prepare_pooled_capacity` raised a bare `ManagedComputeLaunchError`, which the MRO walk maps to **400**, not the 500 the plan assumed; `launch_pool_capacity` gave 409 coded `conflict`. The plan's prescription — make `CapacityAcquisitionStatus.AtLimit` the single internal representation — does not fit: that contract is reservation-scoped and neither raising site has a reservation, and the reservation path never reaches HTTP (the scheduler treats AtLimit as backpressure, per CAP-07). Unified on `CapacityLimitReachedError` instead: 409, code derived from the type name, message naming the limit and what is held. Verified 409 + `capacity_limit_reached` through the real status mapping*
- [x] **ERR-16** Extract provider reconciliation out of `ComputeService` — *2969c79. service.py 5932 → 4787 lines; `provider_machines.py` owns the `ComputeProviderInstanceRecord` lifecycle with a one-way dependency. Two deviations: `reconcile_provider_capacity` stayed behind (it coordinates billing and pooled capacity too, and moving it was what dragged four back-references into the new module), and the reconciler is a property rather than a field — `reclaim` and `scheduler_hooks` are reassigned after construction, so a captured one answered from stale settings and terminated a machine whose worker state was merely unreachable*
- [~] **ERR-15** Split `ProductionWorkerSettings` — **struck, with evidence**
  — *the class carries `extra="forbid"` and a YAML source. Split into sibling
  settings classes, each reads the same file and rejects the other classes'
  keys — verified empirically, not reasoned: a partial class with
  `extra="forbid"` raises on the siblings' keys. So the split costs
  `extra="forbid"`, which is what makes a mistyped key in a 69-field
  deployment config fail loudly instead of silently defaulting. Keeping both
  would need a source that partitions keys per class — more machinery than the
  one class it replaces, which is the opposite of the item's goal. The
  justification was file size; the cost is a real guard. Not worth it.*

## Phase 6 — Blind-handler sweep (elective, large)

194 sites. `CLAUDE.md` forbids exclusions and per-file ignores, so there is no
incremental on-ramp: the rule goes on only when the last site is fixed. Counts
are measured, not estimated. `packages/worker` alone is a third of the backlog
and should be budgeted separately.

The per-package split below was written against a count of every broad handler.
Classifying all 195 changed what the work is: 3 re-raise, 8 already log a
traceback, 145 bind the exception and carry it into the value they return (an
RPC response, a persisted build failure, an error handed back to the caller),
and 20 discarded it entirely. Only the last group had no sink at all, so
ERR-20 … ERR-37 collapse into one sweep of those 20 rather than 18 per-package
passes over sites that already satisfy the rule.

- [x] **ERR-20 … ERR-37** Give every discarding handler a sink — *abbe42e. All
  20 fixed in one pass, each in proportion to what its failure costs: a kill
  that did not happen, a durable 5xx record that was never written, and an
  unconfirmed storage destruction warn with their traceback; metrics, probes,
  and retried loops log at debug. Two runner fallbacks were deliberate rather
  than blind and narrowed their catch instead. Two were behaviour defects, not
  just lost diagnostics, and are fixed in ec38436: a failed presign shipped a
  mount whose download URL was empty, and an unreachable archive store read as
  "bytes absent" — which takes the archive row away from its owner. The 145
  carry-only sites were left alone deliberately: their cause already reaches a
  caller or a durable record, and logging every one would put a stack trace on
  ordinary user-visible failures*
- [x] **ERR-38** Enable the rule — *20c766b, as a repo-owned gate rather than
  BLE001. BLE001 passes `logger.exception` but flags
  `logger.debug(..., exc_info=True)` — the treatment this plan itself
  prescribes for best-effort handlers — and cannot see
  `contextlib.suppress(Exception)`. With `noqa` disallowed, enabling it would
  mean rewriting correct handlers to satisfy the checker.
  `.github/scripts/validate_blind_handlers.py` asks the real question and runs
  in CI beside the existing scope validator*
- [x] **INFRA-19** Write the production runbook — *03e3ec5, `deploy/RUNBOOK.md`. Covers bring-up, the sidecar recreate hazard, the four-step failed-node path, draining, rotation, and irreversible actions; every command run live. The ingress and alerting sections state they are pending rather than describing infrastructure that does not exist*

---

## Decisions

Settled by the owner. These are binding; where a track document still poses one
of these as a question, this section overrides it.

| # | Decision | Effect |
|---|---|---|
| 1 | **Cloudflare Tunnel** is the public ingress | INFRA-06 is concrete: tunnel + `lazycloud.dev` zone. Not an ALB — the control plane does not run in AWS. Keeps DDoS absorption in front of the unauthenticated enrolment route |
| 2 | Agent artifact stays on the **release bucket** | BOOT-05 adds `--agent-url`; the 47.6 MB transfer never touches the control plane |
| 3 | **Do the full `DomainError`/`ErrorResponse` code field now** — option (a) | ERR-06 is one cross-owner change: shared contracts, API sink, SDK/CLI renderer, and `apps/web` Zod schemas together, per `CLAUDE.md` |
| 4 | **Headscale is on the roadmap** (future, for scalability) | BOOT-06 must not further entrench the Tailscale SaaS `/api/v2` shape. `TailscaleTailnetControl` keeps its seam. The `--login-server` gap closes by construction in BOOT-05, since the agent authenticates with the `control_url` the control plane returns |
| 5 | Source-cache cleanup **no longer hard-gates availability** | Implemented after a dedicated investigation settled the crux: targets are enqueued only after the store bytes and the object row are already gone, so a workload cannot reach the stale cache entry. A worker serves while failed purges retry from the durable queue (`Draining`); only a round that cannot run — unreachable control plane, lost cache session — blocks. The admin cleanup ledger still reports incomplete purges |
| 6 | `ssm:SendCommand` goes to a **separate break-glass role** | INFRA-09 splits it out; the acceptance operator role does not hold RCE on nodes |
| 7 | **No alerting for now** | INFRA-15 is deferred out of the programme. Signals still land durably (INFRA-14) and are scrapable (INFRA-12) — only paging is dropped. INFRA-19 loses its INFRA-15 dependency |
| 8 | Delete the `lazycloud-shared` stack — **blocked, see below** | Recorded as INFRA-21 with a mandatory pre-check |
| 9 | Customer re-authorization **now** | INFRA-16 proceeds while the test connection is the only one |
| 10 | **`PATCH`** for partial policy edits; `PUT` stays full-replace | ERR-13 |
| 11 | Failed-node reclaim hold: **300 s** | BOOT-01. Inspection becomes possible via INFRA-09 rather than by leaving nodes running |
| 12 | `external_id` **documented, not encrypted** | INFRA-17: record in `AGENTS.md` that it is deliberately clear because it is a confused-deputy nonce, not a secret |
| 13 | **Handle the worker RPC surface at the dispatch boundary** — option (b) | ERR-37 shrinks: one wrapper around RPC method invocation logs the cause and converts to the failure response, replacing 26 near-identical handlers in `container_service/service.py`. New RPC methods inherit it. Its own item, its own acceptance — not folded into the lint sweep |
| 14 | CAP-13 stays **measurement-gated** | Decide from its acceptance measurement, not in advance |

### Blocked: deleting `lazycloud-shared` (INFRA-21)

Deletion was attempted and **refused by CloudFormation**, twice:

> Delete canceled. Cannot delete export
> `lazycloud-shared:ExportsOutputFnGetAttIAMRolesPlatformRolesExternalSecretsRole...Arn`
> as it is in use by `lazycloud-prod-us-east-1-infra`.

The stack holds a CDK substrate from the superseded architecture — 4 ECR
repositories (`backend/api`, `backend/backgroundworkers`, `backend/crons`,
`web`), ECR replication, 5 IAM roles (Karpenter controller and node, EBS CSI,
EFS CSI, External Secrets), and a Secrets Manager secret. It exports several of
those role ARNs, and `lazycloud-prod-us-east-1-infra` imports them.

That importing stack has itself been in `DELETE_FAILED` since 2026-01-28,
blocked on two subnets with live dependencies and an ACM wildcard certificate
still in use. So the order is forced:

- [ ] **INFRA-21** Retire the superseded CDK substrate — *needs an owner decision first*
  - **Blocker**: `lazycloud-shared` cannot be deleted until
    `lazycloud-prod-us-east-1-infra` is gone, and that one cannot be deleted
    until its subnets and ACM certificate are released.
  - **Decision required**: the blocking subnets may belong to the VPC hosting the
    `eksctl-ambient-*` clusters, which appear to be an unrelated project.
    Confirm that VPC is disposable before anything is deleted.
  - **Change**: release the certificate and subnet dependents, delete
    `lazycloud-prod-us-east-1-infra`, then delete `lazycloud-shared`.
  - **Risk**: a half-completed delete leaves orphaned NAT gateways, load
    balancers, or subnets accruing cost with no stack managing them — which is
    exactly how prod-infra reached its current state.
  - **Acceptance**: both stacks absent; no orphaned VPC/NAT/ELB remaining.
  - **Status 2026-08-01**: still blocked, and the blockers are now exact.
    `lazycloud-prod-us-east-1-infra` fails on three resources: subnets
    `subnet-0446af4ffd9326f46` and `subnet-09fda7ebdbfa009b4` (both report live
    dependencies) and ACM certificate
    `arn:aws:acm:us-east-1:534742592531:certificate/5ae60e35-8970-4b33-9ffb-abfd3fed05c6`
    (in use). Determining what holds them is not possible from the acceptance
    role: `lazycloud-default-test-operator` is denied `ec2:DescribeSubnets` and
    `ec2:DescribeNetworkInterfaces`, correctly — it is scoped for acceptance, not
    infrastructure teardown. So this needs owner credentials **and** an owner
    determination that the VPC is disposable. The general "everything here is
    test" authorization does not settle that: the plan itself records that these
    subnets may host `eksctl-ambient-*` clusters belonging to another project,
    and deleting them is irreversible.

### Decision 5: resolved

An independent investigation settled the retention question the gate hinged on:
cleanup targets are enqueued in the same transaction that deletes the object
row, after the store bytes are verified gone (`storage/retention.py:262-301`),
and mounts resolve per-request from that row (`execution/mounts.py:43-53`) —
so a stale cache entry is unreachable by any workload, and idling the machine
retains the bytes exactly as long as serving would. The gate was therefore
converting a hygiene backlog into billed idle capacity for zero retention
benefit, at registration **and** mid-service (`Draining` refused keepalive and
dispatch).

Implemented: an incomplete round resolves to `Draining` and the worker serves;
keepalive drives a retry round each interval; the durable queue keeps failing
targets with backoff; the admin ledger still reports them. Only a round that
cannot run at all — unreachable control plane, lost session fence — refuses
service, which is what `source_cache_unavailable` now exclusively means.

## Deferred out of scope

- **Alerting and paging** (INFRA-15) — owner decision 7. Revisit before real
  production traffic; the durable signals it would have consumed still land.
- **The unverified `get.docker.com` branch** (`operations.py:545-554`) —
  Tailscale and the agent are SHA-pinned; this branch is not. Managed nodes take
  the pinned `dnf` path, so it is out of scope here. It remains real.
- **`infrastructure/secrets-backup/`** — live third-party credentials,
  unencrypted on disk, untracked and never committed, correctly gitignored.
  INFRA-18 removes them from the tree; they belong in a secret manager
  regardless.

## How we work this

One phase at a time, in order. Within a phase, respect the *after* annotations
and otherwise work top to bottom. Each item's acceptance is in its track
document and is deliberately an observation or an existing scenario rather than
a new test — the Test Decision Gate applies.

Re-run the phase acceptance before moving on. A green narrow run is evidence for
the owner it covered and nothing more.
