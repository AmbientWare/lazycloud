# Todo

This is the ordered backlog. Every implementation task starts here before it moves to
`board/loop.md`.
Tasks remain here when ready, dependent, or blocked; they never appear on another board at the same
time. `../findings.md` remains the authoritative 29-item checklist until every finding is checked.

Each task states what must change and how completion will be accepted. Promote the highest ready
task whose owner scope does not conflict with active work. Finish the current manager-owned task
before selecting another.

## T-050 — Connected-AWS compute reliability redesign

Priority: active program; supersedes the T-033 bootstrap-fragility items and the
runtime-VPC provisioning design.

Source: recurring live-acceptance failures with one shared root cause set — runtime network
construction in the customer account (VPC quota exhaustion surfaced only as scheduler tracebacks),
a silent multi-dependency shell bootstrap (a single Funnel TLS reset killed a paid machine 8
seconds after boot with no durable reason), one 3600-second registration deadline covering the
whole pre-ready window, and a hand-assembled operator activation.

Decision: adopt the standard connected-account design. Network moves into the customer connection
CloudFormation stack (outputs recorded on the connection; VPC-mutation IAM grants removed);
bootstrap becomes heartbeat-first (a verified pre-enrollment phase report reusing the existing
`bootstrap-failure` proof path, `Booting` phase, per-step bounded failure reasons from the shell);
the boot path drops the gateway entirely (install inlined into userdata, agent from release S3);
reclaim gains per-phase deadlines with bounded relaunch attempts and a durable pool
`degraded_reason`; a per-region AMI release stage pre-bakes docker/tailscale/agent/worker-image;
`deploy/compose` gains the board-mandated one-command activation entrypoint; and a dev/test-only
`packages/providers/fake` provider drives the full provision→boot→enroll→ready→reclaim cycle
against real Postgres/Redis so iteration never requires paid AWS.

Known defect folded in: zeroing the workspace compute policy does not scale the canonical warm
pool to zero (observed live: policy zeroed, pool retained desired one and kept billing until the
public pool scale-zero owner was invoked directly).

Checkpoints: (1) fake-provider loop + heartbeat endpoint + per-phase deadlines; (2) network into
the connection stack + single bootstrap-script rewrite; (3) AMI release stage + activation
entrypoint; (4) one live AWS certification through the four `tests/e2e/external/aws` stages —
the only paid run of the program. Full design: `~/.claude/plans/serialized-honking-elephant.md`.

Live certification (2026-07-24, first attempt): stages 1-2 PASS, stage 3 blocked, spend returned to
verified zero (0 instances, 0 volumes, ASG desired 0; connection and stacks retained).

Proven against real AWS: release `t050-live-20260724-1` published and anonymously verified with the
first baked AMI (`ami-0de3546b4282832f3`); connection template `2026-07-24.v11` creates the network
and its VPC/subnets/security-group outputs are recorded on the connection and consumed by pool
provisioning; the launch template carries the rewritten script with zero `install/agent` references;
a machine minted a SigV4-presigned STS proof in pure shell that the production verifier ACCEPTED
(`bootstrap-phase` 200, `booting`), then enrolled (`enroll` 200, `joining`) and reached the warm
baseline (machine `c538885e`, i4i.xlarge, ready=1, 343000 micros/hour). Policy-zero convergence,
bounded relaunch, durable degraded reasons, and per-phase reclaim were all observed operating.

Blocker for stage 3 (paid Function): the worker registers but never becomes available, so the image
build never schedules (`retry-limit`). The machine was correctly refused readiness and reclaimed
("did not become ready"). The same failure REPRODUCES ON THE LOCAL COMPOSE STACK — the local worker
sits `pending` and its worker-slot container is absent — so this is debuggable locally at no cost and
without EC2.

Defects found and fixed this run (all with tests): capacity-image sharing attempted on public
third-party AMIs and on images the target account already owns; `CapacityPoolSizingState.terminal_reason`
and `AwsAccountAuthorizationGeneration.error_message` rejected over-long provider text instead of
truncating, crashing the sizing update; legacy `provider_state.attributes` permanently degraded a pool
because the slimmed model forbade unknown keys; `deploy/compose/activation.py` orphaned the Tailnet
gateway's shared network namespace; and a degraded pool refused enrollment and bootstrap reports,
which is self-reinforcing because each refusal is recorded as another failure. Deployment gaps closed
outside the repo: the customer-stack execution role needed EC2 network permissions for the v11
template (`deploy/connected-aws/customer-stack-execution-policy.json`) and the control role needed
capacity-image permissions (`deploy/connected-aws/control-role-capacity-images-policy.json`); both
operator/control stack templates remain uncommitted, which is a standing gap.

STAGE-3 ROOT CAUSE (2026-07-24, proven on a held machine via SSM): the container worker cannot
resolve its runtime callback host. `compose.yaml` sets
`LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL` to `http://control-plane:9000` — a Compose service name — and
that value reaches the AWS worker slot. Inside the EC2 worker container the name does not resolve, so
`validate-readiness` raises `socket.gaierror: [Errno -2] Name or service not known`, the container
exits 1, and the agent recreates it about every five seconds indefinitely. Captured with
`docker events` (create/start/die exit=1/destroy every ~5s) and `docker logs -f` on the live machine.
The worker therefore never leaves `pending`, no build can schedule, and stage 3 fails `retry-limit`
while the machine still reports bootstrap-Ready — the machine's own phase reports succeed because
they use the public HTTPS Funnel, while the worker uses this internal HTTP URL.

Fix direction: connected-AWS machines must receive a publicly reachable runtime origin (the Funnel
URL already proven to work for phase reports), never a Compose-internal hostname. The worker slot
also carries `worker_image: container-worker:local` — another local-only value that only works today
because the agent's `--worker-image` ECR digest overrides it; both are the same class of defect,
local deployment values leaking into the connected-account path.

Root cause found (2026-07-24, via SSM) — THE stage-3 blocker: the agent was never installed as a
persistent service. Phase 5's rewrite launched it with `exec "$AGENT_BIN" join` inside cloud-init, so
it enrolled, registered a worker, and then died with the cloud-init script module. SSM on a live
machine proved it: zero Docker containers and `systemctl is-active lazycloud-agent` returning
`inactive`, while the platform still showed the machine bootstrap-Ready. That single line explains
every stage-3 failure — worker stranded in `pending`, no container worker, image build `retry-limit`.
The self-hosted path never had this bug because it passes `--background`, which installs a systemd
unit. Fixed: `_bootstrap_script` now writes `/etc/systemd/system/lazycloud-agent.service` and runs
`systemctl enable --now`, with a golden-file assertion that `exec "$AGENT_BIN" join` never returns.

Second defect from the same review (owner-raised): the unit copied the shared template's
`Restart=on-failure` with `StartLimitIntervalSec=300`/`StartLimitBurst=5`, which gives up permanently
after roughly two and a half minutes of control-plane unreachability — shorter than observed gateway
outages, so a machine would still end as billed dead capacity. Now `Restart=always` with
`StartLimitIntervalSec=0` and a 15-second backoff, so a machine retries for as long as it exists.
NOTE: `render_systemd_unit` in packages/agent/src/agent/service_manager.py still carries the original
rate limit for the self-hosted install path and likely has the same latent failure.

Both fixes are unverified against AWS: the run that followed them executed entirely while the local
Tailnet Funnel was returning 000, so no machine could report regardless of agent lifetime. The
Funnel repeatedly loses its public path when Compose containers are recreated (defect #9 is fixed in
`deploy/compose/activation.py`, but manual rebuilds still trigger it). Stabilizing that local gateway
is the prerequisite for the next attempt; AWS ended at verified zero (0 instances, 0 volumes,
connection removed).

Local reproduction attempt (2026-07-24, after certification): the local container worker ALSO fails
to register, but for a DIFFERENT, macOS-specific reason — the agent starts worker slots with
`--network host`, and under Docker Desktop for Mac that namespace has an address but no IPv4 default
route, so `CommandNetworkSystem.discover_host_capabilities` raises "worker host has no IPv4
default-route interface" (packages/worker/src/worker/network_backend.py:321) and the worker container
crash-loops. The route parser and the `lazycloud_default` bridge are both correct; only host
networking on Docker Desktop lacks the route. Consequence: container workers cannot run end-to-end on
a macOS host, so local acceptance of the worker path needs a Linux host, a Linux VM, or a bridged
worker network. This is NOT the AWS failure — that machine's worker remains undiagnosed and needs
on-machine access.

Fixed during the attempt: the agent's worker-slot stop treated a concurrent Docker removal
("removal ... is already in progress") as an error and retried forever, so a slot was never recreated
and its worker never left `pending` (apps/agent/src/agent_app/daemon.py, regression test in
apps/agent/tests/test_agent_worker_configuration.py). Disproved hypothesis, recorded so it is not
retried: a zero-capacity AWS policy does NOT disable local agent workers — the same failure occurs
with a non-zero policy.

Next: debug worker readiness on the local compose stack (free, full shell), enable SSM on the node
role so machines are diagnosable, and extend the phase-report channel to carry worker network-probe
failures. Then rerun the four stages as one clean pass.

Progress (2026-07-24): checkpoints 1-3 are implemented and locally accepted. Template
`2026-07-24.v11` owns the network (managed pool provisioner shrank 1905 to 1299 lines; VPC-mutation
IAM grants removed); the bootstrap script is gateway-free with shell-minted SigV4 STS proofs proven
byte-identical to botocore, phase reports, and bounded failure reasons; internal pools reclaim under
the capacity lease with per-phase 300-second deadlines, bounded relaunch, and durable
`degraded_reason`; zero-capacity policy updates now drive pool desired to zero through the guarded
scale owner; `deploy/compose/activation.py` and `deploy/ami/bake.py` exist with release manifest
`capacity_cpu_ami_ids` and connection-time AMI sharing. The fake-provider devloop
(`tests/e2e/local/compute_fake`) proves ready, fail, and silent modes end-to-end against real
PostgreSQL/Redis: silent bootstrap now converges to reclaim, three bounded relaunches, and a
degraded pool in four cycles. Integrated batch: 319 tests green across compute, providers/aws,
provider-clients, shared, api, lazycloud, and deployment owners. Remaining: checkpoint 4 — publish
the v11 release (with AMI bake), reconnect through the new-generation stack, and run the four-stage
live certification.

Certification attempt (2026-07-24, second session): the four-stage run finally reached the data
plane, and the cause of every previous `retry-limit` was found. `compose.yaml` defaulted
`LAZYCLOUD_IMAGE_BUILD_CONTAINER_POOL_SELECTOR` to `default`, which attaches every image build to
the local pool and silently overrides the workspace policy, so on a connected-AWS deployment builds
queued for local capacity that cannot exist until the schedule retry limit. Proven by capturing the
queued request: `pool_selector="default"`, `capacity_owner_id` of the local pool,
`placement_source="attached_pool"`. With the Compose default emptied, a build was placed on the AWS
worker and executed for the first time; it then failed on a managed-package digest mismatch between
the locally built control plane and the published worker image, which a matching publish resolves.

Also fixed and committed: transport failures were not classified recoverable anywhere
(`HttpTransportError` is a plain `RuntimeError`), so a single gateway TLS reset killed the agent,
and the unit's start rate limit then stopped restarting it — a machine billed for 30+ minutes with
a dead agent. The AWS bootstrap wrote a second copy of the agent unit that disagreed with the one
the agent installs, so the restart-policy fix made there never reached a machine; the bootstrap now
calls `install-service` and `render_systemd_unit` is the single owner. The scheduler now reports the
pool selector, schedulable worker count, and first failing fit predicate instead of a bare
`retry-limit`, and that detail reaches the user through the existing
`image_build_scheduling_failure` evidence path.

Disproved, recorded so it is not retried: the worker source-cache stall was NOT a separate defect.
Restarting the agent produced the first ever `toggle-worker-available` call and the first
`available` cache generation — the stall was entirely downstream of the agent crash loop. Image
builds are also NOT unbounded: `fail_stale_active` reaps builds whose 120-second claim lease
expires. The narrower real gap is that staleness is checked only when claiming, so a client that
already attached to an active build follows it indefinitely if that build's driver dies.

AWS ended at verified zero: 0 instances, 0 ASGs, 0 volumes, both `compute-connection-*-g1` stacks
deleted, all platform stacks preserved.

## T-051 — Rebuild connected-compute testing around a fast local loop

Priority: ready; supersedes the T-005 four-stage acceptance and folds in T-037. Blocks the next
paid AWS run.

Source: proving connected compute has cost far more than building it. Of the defects found across
two certification sessions, nearly all were provider-neutral — a Compose default, a retry
classification, a rendered unit, duplicate ownership, placement resolution — yet each was
discovered on paid AWS machines through Redis/Postgres archaeology and SSM. Live AWS became the
debugger because there is no data-plane loop: `tests/e2e/local/compute_fake/devloop.py` covers
machine lifecycle only and composes services in process, so it structurally cannot catch a
deployment-configuration bug, and container workers cannot run on macOS at all (worker slots use
`--network host`; Docker Desktop's host namespace has no IPv4 default route).

Outcome: three tiers with explicit ownership. Tier 1 owner tests for pure decisions. Tier 2 — the
missing centrepiece — a data-plane loop on a Linux host (the ml-machine) against the real Compose
stack with `provider_fake` registered as the workspace's connected provider, driven through the
public SDK, proving policy resolves to the provider pool, a build is placed there and executes, a
Function returns its exact result, and teardown leaves no residue. Running against the real
deployment is the point: an in-process harness would have missed the Compose bug. Tier 3 — one
paid AWS run proving only what AWS alone can: CloudFormation template/role acceptance, IMDSv2+STS
identity, EC2 launch/tag/terminate, egress from the customer VPC, and one minimal Function.
Certification runs published artifacts on both control plane and worker so managed-package digests
match by construction and version skew cannot recur.

Acceptance: Tier 2 green twice in a row on the ml-machine (repeatable and self-cleaning); the four
AWS scenario modules collapsed into one certification module taking release inputs from the
deployment environment rather than hand-passed arguments; then one AWS certification pass ending at
verified zero.

## T-032 — Persist public CLI profiles as secret-bearing state

Priority: after the active connected-AWS acceptance

Source: live-acceptance preflight found the existing `~/.lazycloud/config.yaml`, which contains
bearer tokens, was mode `0644`.

Description: Make the public SDK/CLI profile owner write its token-bearing configuration through an
atomic, symlink-safe mode-`0600` replace. Existing unsafe files must fail closed or be repaired only
through an explicit ownership-safe path; profile reads and updates must never echo token values.

Completion:

- Fresh profile creation and every update leave the file exactly mode `0600` with a durable atomic
  replace, and reject a symlink, non-regular file, concurrent replacement, or unsafe parent target.
- Focused config and public CLI tests prove multi-profile preservation, token redaction, failure
  recovery, and no plaintext in command output or error text.
- The existing local profile file has been restricted to `0600` for the current live acceptance;
  that operational repair does not substitute for the product fix.

## T-030 — Publish immutable connected-AWS release inputs

Priority: blocked until the concurrent T-017 checkpoint is committed, then before T-005

Source: T-005 redesign after the live gate proved that building release infrastructure inside an
acceptance test creates a second deployment system.

Description: Use the existing production release workflow to publish one immutable Linux `amd64`
agent artifact, container-worker image digest, and account-authorization CloudFormation template.
Configure the Compose control plane from that release manifest before live provider acceptance.
Release S3/ECR infrastructure is durable release ownership and is not created or deleted by a test.

Checklist:

- [x] Deploy the documented durable release stack with its versioned public bucket and ECR
  repository. GitHub OIDC publication is optional and all-or-none; local publication uses the
  explicitly authorized AWS profile without inventing repository trust.
- [ ] Build, publish, and anonymously verify the agent artifact, worker image digest, template, and
  manifest through `.github/workflows/release.yml` or its documented local production commands.
- [ ] Configure only the affected Compose services from the immutable manifest and verify the
  control plane reports the exact agent version, checksums, template URL, and worker image digest.
- [ ] Keep release credentials out of Compose, the client profile, logs, and acceptance files; no
  mutable image tag or local build may satisfy this task.

Acceptance:

- Release-focused tests and verification pass, an Amazon Linux node can download and verify the
  agent, and the worker image is anonymously inspectable by immutable digest.
- The release resources remain after T-005 and are managed only by the release workflow.

Blocker: release ownership and Compose activation are checkpointed at `f16b89fd` and `39b3875e`,
and the public acceptance replacement is checkpointed at `f38ae1a7`. The finished T-017 scaling
implementation is still mixed in shared runtime files with unapproved, internally inconsistent
T-003 image-retention work. Publishing that tree would make immutable bytes from an invalid release
boundary. A disposable clean worktree is isolating and accepting only T-017; no registry login,
image push, or S3 publication occurs until that commit-shaped tree passes.

## T-024 — Publish a full document-processing ASGI example

Priority: blocked live acceptance

Source: owner-selected Modal-inspired example, implemented originally on LazyCloud.

Description: The FastAPI ASGI app, static UI, bounded uploads, durable OCR Tasks, signed job
tokens, Volume storage, guide, and focused checks are implemented.

Completion:

- The public CLI workflow passes against current Compose from upload through real Tesseract OCR,
  result deletion, and exact deployment/Volume/secret cleanup.

Blocker: the shared Compose database predates the current source-cache schema and another active
owner is using the stack. Recreating or mutating it here would violate that owner's scope.

## T-026 — Publish a parallel Parquet/S3 example

Priority: blocked live acceptance

Source: owner-selected Modal-inspired example, implemented originally on LazyCloud.

Description: The CloudBucket seed/read workflow, typed TaskQueue fan-out, strict key validation,
whole-batch failure, JSON summary, guide, and focused checks are implemented.

Completion:

- Compose object-store acceptance proves seed/read/fan-out/write/failure/cleanup on a current stack.
- Real S3 acceptance runs when explicitly authorized and records exact object cleanup.

Blocker: the shared Compose stack is stale and actively owned elsewhere, and no real-S3 credentials
or spend authorization were provided. No local storage or fake Task path substitutes for acceptance.

## T-027 — Integrate the Mintlify examples catalog and canonical links

Priority: blocked on hosted documentation origin

Source: owner request that repository examples and every example link resolve through Mintlify.

Description: The Examples landing page, five guides, `docs.json` navigation, repository README,
source/page parity tests, broken-link validation, and successful local preview build are complete.

Completion:

- Confirm the hosted Mintlify origin, then point the marketing Docs navigation and homepage example
  cards at the canonical example pages and cover those exact targets in tests.
- Inspect the built catalog and guides at desktop and mobile widths.

Blocker: no hosted Mintlify origin or public repository origin is configured in this checkout, and
the local in-app browser was unavailable for visual inspection. Do not invent either public URL.

## T-001 — Complete container-pinned sandbox cleanup acceptance

Priority: blocked on T-003

Source: F-20 current-schema sandbox workflow.

Description: The container-pinned URL, bounded proxy failure, current-schema database, Compose/Helm
bootstrap, and automatic administrator bootstrap slices are implemented and checkpoint-ready. Run
the final public sandbox lifecycle and exact cleanup proof after T-003 establishes durable published
image archive ownership.

Completion:

- Two same-stub sandboxes retain distinct pinned routes across restart and sibling termination.
- Cross-workspace, stub, port, exposure, and stopped-container access fail without rerouting.
- Workspace deletion leaves no reusable authorization, database state, object-store artifacts, or
  worker cache while preserving any globally retained image bytes required by a surviving consumer.

Blocker: T-003 must close published image archive ownership and reference retention before the
workspace deletion proof can distinguish a leak from required global data.

## T-003 — Make published image archives globally owned and reference-retained

Priority: highest after the non-blocked portion of T-001

Source: current-schema workspace-deletion blocker discovered during sandbox acceptance.

Description: Transfer immutable content-addressed image archives to system/global ownership when
publication succeeds. Workspace deletion removes that workspace's authorization, source, and build
references but does not delete bytes still referenced by another live consumer. Retention deletes
the global image and archive together only after references and the accepted retention window end.

Completion:

- A deleted workspace has no reusable image authorization or build/source reference.
- Surviving consumers retain a valid archive; no active global image row can point to missing bytes.
- Concurrent publication/deletion, failed ownership transfer, reference removal, and retention are
  idempotent and leave neither unauthorized access nor leaked unreferenced archives.
- Current Compose workflow proves invocation before/after workspace deletion and exact database,
  object-store, worker-cache, and authorization cleanup.

Decision: the global immutable archive contract is recommended because the existing image identity
and deduplication model is global. This materially sets security, storage-cost, and deletion
behavior and requires owner confirmation before implementation.

## T-007 — Split concentrated backend owners by durable state machine — F-16

Priority: after persistence and source-cache ownership settle

Source: `findings.md` F-16.

Description: Remeasure the scheduler state, worker-repository mutation planes, autoscaling
controllers, and compute lifecycle after transitional paths are gone. Split only along real durable
aggregate, authority, transaction, and reconciliation boundaries.

Completion:

- Focused owners expose precise dependencies and no forwarding mega-facade or import aggregator.
- No split is justified only by line count.
- Current dispatch, container, scaling, provider, cancellation, restart, and cleanup workflows keep
  their accepted behavior.
