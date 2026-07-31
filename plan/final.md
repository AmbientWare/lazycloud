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

- [x] **CAP-01** Guard `_acquire_from_controller` against pending-worker reservations
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
- [ ] **ERR-07** Stop flattening `ManagedComputeLaunchError`; stop returning 500 for a quota hit — *after ERR-06*
- [ ] **ERR-08** Record why a lease was lost instead of only that it was
- [ ] **ERR-09** Give the shell compensation path a reason
- [ ] **ERR-13** Move compute-policy editing server-side — *after ERR-06*
- [ ] **ERR-14** Stop billing a workspace that zeroed its CPU capacity
- [ ] **CAP-02** Carry a typed reason on every transition to `Unavailable` — *after ERR-12*
- [ ] **CAP-03** Report which registration step failed — *after CAP-02*
- [ ] **CAP-04** Remove the worker record when registration never completed — *after CAP-03*
- [ ] **CAP-05** Stop discarding the keep-alive source-cache outcome — *after CAP-02*
- [ ] **CAP-06** Give `mark_available` one job — *after CAP-03*
- [ ] **CAP-07** Reclassify "at limit" as backpressure
- [ ] **CAP-08** Introduce one owned readiness predicate
- [ ] **BOOT-03** Report bootstrap phases from the agent, not only from user-data
- [ ] **INFRA-11** Emit HTTP request metrics from the gateway middleware
- [ ] **INFRA-12** Make `/metrics` scrapable and correctly typed — *after INFRA-11*
- [ ] **INFRA-13** Carry a bounded diagnostic excerpt on the bootstrap-failure report — *after BOOT-03*
- [ ] **INFRA-14** Route capacity and enrolment error paths through the durable event channel

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

- [ ] **INFRA-01** Add a Redis rate-limiter primitive to coordination
- [ ] **INFRA-02** Resolve the client address from exactly one configured source
- [ ] **INFRA-03** Stop calling AWS inside the provider-node request path — *after INFRA-01*
- [ ] **INFRA-04** Bound the STS proof verification so it cannot exhaust the API — *after INFRA-01, INFRA-03*
- [ ] **INFRA-05** Rate-limit every unauthenticated route before exposure — *after INFRA-01, INFRA-02*
- [ ] **INFRA-17** Close the remaining plaintext durable-secret gaps

**Phase 2 acceptance.** Load-test `/gateway/provider-nodes/*` with an unverified
pool UUID and demonstrate: no outbound AWS call occurs before identity
verification, no request occupies a threadpool worker for longer than the
configured bound, and the rate limiter sheds excess. This is a load test, not a
unit test.

## Phase 3 — Public ingress

- [ ] **INFRA-06** Stand up the public HTTPS ingress — *after INFRA-03, -04, -05*
- [ ] **INFRA-07** Refuse to launch managed capacity against an unreachable public origin — *after INFRA-06*
- [ ] **INFRA-15** Scrape, alert, and page on the tracks' failure signals — *after INFRA-12, CAP-02, CAP-08*

**Phase 3 acceptance.** An EC2 instance in the connected account reaches the
ingress and completes enrolment. Sustained load equal to a full pool launch does
not degrade it — Funnel failed exactly this test, which is why it is not the
answer here.

## Phase 4 — Bootstrap simplification

The payoff. Deletes the pool bootstrap key, the tailnet-first boot path, the
duplicated bash SigV4, and roughly 350 lines of shell.

- [ ] **BOOT-04** Point managed-pool nodes at the public control-plane origin — *after BOOT-03, INFRA-06, Gate A*
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

- [ ] **CAP-09** Stop asserting durable `Worker.status = Running` at registration — *after CAP-08*
- [ ] **CAP-12** Split the persisted bootstrap phase from the derived verdict — *after CAP-08, BOOT-09*
- [ ] **INFRA-20** Regenerate the Alembic baseline *(added in synthesis; see Gap above)*
- [ ] **CAP-10** Delete `Worker.version` — *after CAP-09, INFRA-20*
- [ ] **CAP-13** Delete the pending-worker reservation path — *after CAP-01; requires the restart measurement*
- [ ] **CAP-14** Collapse acquisition to one idempotent entry point — *after CAP-13*
- [ ] **CAP-15** Delete `CapacityPoolSizingState` and the `pools.sizing_*` columns — *after CAP-14, INFRA-20*
- [ ] **CAP-16** Collapse the reservation status machine — *after CAP-15*
- [ ] **CAP-17** Split `capacity_reservations.py` along its five-way seam — *after CAP-16*
- [ ] **ERR-10** Collapse three at-limit conventions into one — *after ERR-07, CAP-14*
- [ ] **ERR-16** Extract provider reconciliation out of `ComputeService` — *after ERR-03, ERR-10, CAP-14*
- [ ] **ERR-15** Split `ProductionWorkerSettings`

## Phase 6 — Blind-handler sweep (elective, large)

194 sites. `CLAUDE.md` forbids exclusions and per-file ignores, so there is no
incremental on-ramp: the rule goes on only when the last site is fixed. Counts
are measured, not estimated. `packages/worker` alone is a third of the backlog
and should be budgeted separately.

- [ ] **ERR-20** `packages/foundation` — 1
- [ ] **ERR-21** `packages/providers/aws` — 1
- [ ] **ERR-22** `packages/observability` — 2
- [ ] **ERR-23** `packages/storage` — 2
- [ ] **ERR-24** `packages/storage-client` — 2
- [ ] **ERR-25** `apps/agent` — 4
- [ ] **ERR-26** `packages/gateway` — 4
- [ ] **ERR-27** `packages/lazycloud` — 4
- [ ] **ERR-28** `packages/control` — 7
- [ ] **ERR-29** `apps/container-worker` — 8
- [ ] **ERR-30** `packages/worker-repository` — 8
- [ ] **ERR-31** `packages/runner` — 9
- [ ] **ERR-32** `packages/compute` — 12 — *after ERR-16*
- [ ] **ERR-33** `packages/images` — 13
- [ ] **ERR-34** `packages/execution` — 14 — *overlaps ERR-09*
- [ ] **ERR-35** `apps/api` — 18
- [ ] **ERR-36** `packages/scheduler` — 22 — *overlaps ERR-08*
- [ ] **ERR-37** `packages/worker` — 63 — *budget separately*
- [ ] **ERR-38** Enable the rule — *after ERR-20 … ERR-37*
- [ ] **INFRA-19** Write the production runbook — *after INFRA-06, -09, -13, -15*

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
| 5 | Source-cache cleanup **no longer hard-gates availability** | CAP-06: bound by attempt count, let the worker serve while cleanup retries, record the failure durably. One un-purgeable object must not strand a machine |
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
