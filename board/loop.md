# In Progress

This is the working board. Tasks appear here only while someone is actively implementing or
accepting them. Every task entered through `board/todo.md`; completed tasks move to
`board/finished.md`, and inactive or blocked tasks return to `board/todo.md` with the blocker
recorded.

## Working rules

- Default to one manager-owned task at a time. Parallel tasks require disjoint owners and files.
- Until every `findings.md` item is closed, integrate and continue implementation in the main
  worktree; resume isolated feature worktrees only after the findings program is complete.
- Keep the current contract, next action, and remaining acceptance here; replace stale progress
  instead of appending a command diary.
- Use iteration checks during edits, checkpoint checks after a coherent owner slice, and release
  checks only for a release or broad parity claim.
- `../findings.md` remains the authoritative checklist until all 29 findings are checked.

## Current execution order

0. **Active — T-050 (supersedes T-033 bootstrap/runtime-VPC items):** connected-AWS compute
   reliability redesign — network into the connection stack, heartbeat-first bootstrap with
   per-phase deadlines and bounded reasons, gateway-free boot path, AMI release stage, one-command
   activation, and a fake-provider dev loop. See `board/todo.md` T-050. The 2026-07-24 live run
   proved the failure classes: VPC quota exhaustion surfaced only in scheduler logs, a Funnel TLS
   reset silently killed a paid machine 8s after boot, the 3600s registration deadline left it
   billing unnoticed, and zeroing the workspace policy did not scale the canonical pool to zero.
1. **Superseded in part — T-033 / T-005:** make the production agent-managed worker path the
   canonical local Compose path, then reduce connected-AWS acceptance to independent
   account-connection, one-machine, portable Function, and cleanup stages.
2. **Parallel — T-036:** close the Helm administrator/workspace bootstrap owner so Kubernetes worker
   certification can install deterministically without manual database or token repair.
3. **Parallel — T-037:** replace the isolated full-stack capacity harness with one thin public worker
   scenario against a healthy root Compose stack.
4. **Implementation accepted — T-017:** the coherent scaling/preemption runtime and immutable AWS
   release are published; T-036 and T-033/T-005 now own deployment/provider certification rather
   than blocking ordinary worker iteration.

T-004 remains design-blocked; older implementation evidence below describes completed checkpoints,
not the current release state.

## T-033 — Make real-AWS acceptance reusable and role-based

Owner: root

Priority: active; required before resuming the paid T-005 stage.

Superseding architecture decision (2026-07-23): local Compose is the primary integration and
acceptance environment. A provider owns only machine creation, provider identity, observation, and
deletion. Every machine then enrolls through the same provider-neutral agent boundary, receives the
same desired worker-slot contract, launches the same immutable container-worker image, and uses the
same scheduler, runner, storage, logs, results, and cleanup owners. Local/self-hosted enrollment
uses a one-use join credential; AWS enrollment retains verified instance identity. Connectivity may
use the Compose network, a private network, or an overlay, but the authenticated gateway and worker
contracts do not vary by provider.

The canonical readiness invariant is provider machine present plus authenticated agent online plus
desired worker registered and healthy. EC2 or Auto Scaling health alone is pending, never ready.
Provider-instance state exposes a closed bootstrap phase, bounded safe reason, and observed time.
AWS launch failures become durable before replacement; repeated failures are bounded instead of
silently cycling until an E2E timeout.

Implementation order:

- [ ] Freeze and integrate the provider-neutral launch/readiness/status contracts across shared,
  compute, persistence, API, SDK/CLI, and agent owners.
- [ ] Replace the default direct Compose worker with an agent-managed local node that launches the
  exact production worker image through Docker; keep direct-worker startup only as an explicit
  owner-development profile.
- [ ] Align AWS pooled capacity with the same readiness contract and fail-fast diagnostics.
- [ ] Replace fixed injected AWS sessions with the SDK credential chain for long-running provider
  clients; ambient customer credentials remain outside the control plane.
- [ ] Accept the portable public Function locally before any paid provider run.

Current decision (2026-07-22): finished T-049 supersedes the one-command environment harness,
test-created certifier IAM user/access keys, local resume state, and bundled
`setup/test/status/teardown` flow described in the historical notes below. `AWS_PROFILE=default`
remains bootstrap-only; production and Compose use the exact non-root control role with short-lived
credentials. Acceptance now calls the independent modules under `tests/e2e/external/aws/` and
passes explicit public identities between stages. Release, Compose activation, customer
CloudFormation, public connection, paid execution, evidence, stabilization, and teardown remain
separate owners.

Pre-AWS review decision (2026-07-23): do not run another paid stage until the complete flow is
locally ready as one recoverable state machine. Activation starts and authenticates the exact
Tailnet gateway, proves its external HTTPS health, records the short-lived control-session expiry,
and refuses a paid handoff without enough remaining cleanup time. Connection setup always validates
retained connections, detects obsolete managed templates, and returns the exact stack identity.
The paid stage takes a caller-selected unique run ID and app slug, submits through the public SDK,
and emits the durable task identity immediately. Read-only evidence then polls exact task,
container, pool-machine, worker-version, EC2, cost, and Tailnet identities. Stabilization validates
that ownership, accepts AWS's normal asynchronous desired-zero transition, and converges public
cost, EC2, ASG, EBS, and the exact agent device to zero. Teardown is interruption-idempotent and
cannot succeed until the exact stack and every workspace-tagged managed resource are absent.

The acceptance image contract is Linux `amd64` from authoring through cache identity, scheduler
request, Buildah execution, and archive publication; an ARM host cannot publish a native ARM cache
entry for the AMD64 AWS worker. Selecting AWS as the workspace default creates and maintains one
canonical warm CPU pool through the production policy, compute, scheduler, and provider owners.
The E2E stage never provisions provider infrastructure directly. Release, activation, connection,
paid submission, evidence, stabilization, and destructive teardown remain independently callable.

Pre-live gate closure (2026-07-23): every stage uses one absolute deadline and the supported target
timeout is at most 900 seconds. Because the approved `default-test` profile is role-chained and its
Compose control session is capped at one hour, connection setup and paid execution use separate
activations. Immediately after the connection becomes ready, the operator reapplies the exact same
immutable release and reruns local readiness; readiness requires enough credential lifetime for
paid submission, evidence, stabilization, and a ten-minute cleanup reserve. The customer
CloudFormation action must match the activated template URL and platform principal exactly.
Caller-selected run/app identities make paid submission recoverable without duplication. Evidence
emits durable recovery identity before terminal failure, and stabilization can derive omitted
app/pool/machine/device identity only when unique, refusing ambiguity. Destructive teardown requires
the exact machine identity needed to prove Tailnet absence; when no machine ever became durable,
stabilization still proves zero cost but retains the connection and stack for diagnosis.
The role-backed acceptance operator is also the customer action owner: it may pass only the
stack-owned CloudFormation execution role and may create/delete only
`compute-connection-*-g*` stacks from the exact verified template URL. The execution role can
manage only `compute-connection-*` IAM roles. Both retain read-only acceptance inventory while
direct EC2, Auto Scaling, EBS, network, and Tailnet mutation remains denied. The existing
operator-profile stack must be updated from the reviewed template and its execution-role ARN
recorded in the protected acceptance environment before preflight.
The generated customer role carries the ownership tag required by that execution role. Historical
unbound customer stacks are replaced through public reconnect; predecessor cleanup is restricted
to the exact public stack ID and the dedicated execution role. Before activation, the operator
builds every Compose-owned image because activation deliberately starts the complete root stack
with `--no-build`, then requires that stack
healthy before the credential refresh. The Tailnet gateway applies its checked-in Funnel Serve
configuration only to its own tagged device, persists that exact device across forced recreations,
authenticates only when necessary, and restarts after isolated failure. Readiness verifies the exact
device, hostname, proxy target, and public HTTPS origin. Before paid capacity, a local Managed
invocation builds and executes the exact plain Python 3.12 image used by the AWS probe, proving and
warming its Linux `amd64` archive through the connected object-transfer path.

Local readiness closure (2026-07-23): three independent audits reviewed the release, IAM,
activation, S3/archive transfer, readiness/prewarm, zero preflight, connection/replacement, paid
submission, evidence, stabilization, teardown, and recovery handoffs. The current template is
version `2026-07-23.v10`. A deliberately non-publishable linux/amd64 candidate is staged at
`dist/connected-aws/t033-20260723-candidate.1/manifest.json`; its canonical manifest, template,
mode-0755 amd64 agent, worker image architecture, runtime tools, object size, and SHA-256 contracts
all validate locally. The worker Dockerfile now compiles the Go supervisor on the native build
platform while cross-compiling the target binary, avoiding the ARM/QEMU Go compiler crash exposed
by the exact amd64 build.

Connected Compose is restart-safe: local Garage and its bucket bootstrap have fixed local
coordinates and cannot inherit the connected S3 bucket, endpoint, region, or session. The bounded
acceptance session reserves five minutes for submission, fifteen minutes each for evidence and
stabilization, ten minutes for cleanup, five minutes for readiness, and the bounded Compose apply;
readiness and every later stage recheck the actual remaining lifetime. This fixed injected session
is intentionally only a watched acceptance mechanism. A long-running production Compose
deployment still requires a renewable SDK/workload-identity credential owner.

Local evidence: lock, Ruff/format, whole-project basedpyright, Compose rendering, the full local
image build, exact linux/amd64 worker build/runtime probe, canonical release validation, and
live-gate blocking are green. Focused batches include 172 default-environment tests, 54
real-Redis tests, 5 isolated-PostgreSQL schema/bootstrap tests, 49 release/worker-build tests, and
15 session-budget tests. The background prewarm owner now quiesces before database disposal; the
real-Redis suite no longer exposes the shutdown race.

Live acceptance status (2026-07-23): the final immutable release
`t033-live-20260723-754deb6bee4a` is published and independently verified. The approved root
bootstrap created the dedicated `lazycloud-test` IAM user with only permission to assume the exact
operator role; `default-test-source` now uses that identity. The operator and control stacks are
current, the control stack is termination-protected, and direct AWS inventory remains zero EC2,
zero desired ASG capacity, and zero managed EBS. The Tailnet policy now owns the exact agent and
control-plane tags, restricts Funnel to the control-plane tag, and the canonical tagged gateway
passes direct Tailnet and public Funnel health. A fresh Compose baseline initialized the current
schema, activated the short-lived AWS control session, built the plain Python 3.12 AMD64 image, and
published its verified archive through the connected S3 path.

Local prewarm closure (2026-07-23): execution of the AMD64 image on the ARM Compose worker exposed
an invalid managed-runtime ownership assumption. The worker now owns immutable runtime artifacts by
Python version and target Linux architecture, propagates the scheduler's frozen architecture into
OCI planning, and verifies the selected artifact inside the target interpreter. The local
acceptance workload also moved out of the intentionally excluded `tests/` source tree into the
normal user source surface. The exact public Function workflow reused the published AMD64 image,
returned `python:3.12`, and deleted its uniquely owned app. Connected activation now applies the
complete root Compose stack with the temporary session, so the same command owns fresh startup and
credential refresh while Compose preserves unchanged services. A fresh v10 database, bounded
policy, zero preflight, first-generation customer stack, full readiness, connected-S3 image
publication, and local prewarm all pass.

Live blocker (2026-07-23): the obsolete `provider-acceptance` pool was independently proven empty
and removed by its exact ownership tags, freeing the regional VPC quota from five of five to four
of five without touching default, project, SageMaker, or production infrastructure. Function
invocation arguments now persist as a bounded typed projection beside the canonical cloudpickle
payload, while container assignment remains solely in `Task.container_id`; the next real task
proved public `args == ["direct-20260723-01", 21]` and empty user kwargs.

The first retry exposed and closed a dispatch ownership race: allocation consumption preceded
registration of the acquired unit to its exact worker, so reconciliation released the apparently
abandoned provider unit. Final dispatch now performs that ownership transfer under the existing
capacity-owner lease. Authoritative worker and machine deletion also provide the missing
fail-closed terminal path for idempotent app cleanup after a lost worker. A second live run held the
Auto Scaling Group at desired one after the worker enrolled, proving that race fixed.

Capacity contract closure (2026-07-23): provider acquisition and worker scheduling now have
separate canonical shapes. The immutable acquisition shape retains the nominal provider offer for
idempotent planning, acquisition, retry, billing, and release; registration records the exact
worker-reported schedulable CPU, memory, and GPU totals without rewriting that provider contract.
Allocation reuse and placement use the schedulable shape after registration, and reservation
binding requires both the exact worker and exact machine identity when either is already known.
The representative real-Redis lifecycle proves an 8,192 MiB acquisition can bind and schedule on
a 7,900 MiB worker while retaining 8,192 MiB at the provider boundary and rejecting a different
machine. All 31 capacity-owner tests plus focused Ruff, format, and BasedPyright checks pass.
The prior failed task/app are cancelled and deleted with zero active compute and cost. Preserve the
stateful ready connection while the integrated control plane is rebuilt. The next guarded step is
to select AWS as the workspace default, observe the one-worker warm baseline, run one public
Function on that exact machine, and prove the identical baseline after app deletion.

External E2E contract closure (2026-07-23): the live connected-AWS acceptance is one scenario
against an operator-prepared release, healthy control plane, ready public connection, and dedicated
test workspace. Its only product input is one public Python Function call with a unique marker and
the value `21`. Success requires the public task to complete, decode to the marker and `42`, retain
the marker in public logs, exit its container successfully, and map that machine to the approved
connected-AWS account, region, instance type, and bounded positive cost. Agent versions, release
manifest fields, CloudFormation phases, Tailnet readiness, internal pool ownership enums, and
schema-shape duplication are not Function E2E assertions; their production owners and focused
checks retain those contracts.

The workspace policy and compute/provider owners create and maintain one canonical warm AWS CPU
pool before the scenario starts. The SDK creates only the app/task. The scenario mutates only
through the public SDK and CLI: it deletes its exact app and leaves the pool's authoritative
minimum untouched. Direct AWS and Tailnet access is read-only cleanup corroboration. Reusing the
same run ID recovers one matching durable task instead of submitting a duplicate. Success, task
failure, timeout, and ordinary interruption all enter the same cleanup path; cleanup cancels live
work, deletes the exact app, and must prove the public pool, cost, AWS EC2/ASG/EBS inventory, and
Tailnet device set returned to the exact pre-submit warm baseline with no extra unit. Only explicit
connection teardown removes that baseline and proves global zero. Ambiguous identities fail closed.
Release activation, connection create/reconnect, and destructive connection teardown are
deployment/operator workflows, not E2E test stages.

The single acceptance command is:

`uv run --env-file .env python -m tests.e2e.external.aws.connected_aws_function --run-id <id> --app-slug <slug>`.

Invoking that explicitly named external scenario authorizes its one bounded paid Function. The same
command with `--cleanup-only` recovers the exact run and restores the authoritative warm baseline
without submitting work; redundant live and paid-confirmation environment switches do not exist.

External E2E restructure (2026-07-24): the four independent stages now exist under
`tests/e2e/external/aws/` — `account_connection`, `one_machine_readiness`, `connected_aws_function`
(the paid scenario above, including `--cleanup-only` recovery through the durable public `args`
marker), and `cleanup`. CloudFormation customer-action automation moved out of the test tree into
the deployment-owned `deploy/connected-aws/customer_stack.py` (`apply`/`remove`), which validates
the action through `provider_aws.customer_actions` against the exact verified template URL and
platform principal and may pass only the stack-owned execution role; `account_connection` and
`cleanup` invoke it and therefore now require `--template-url`/`--platform-principal-arn` inputs
from the protected acceptance environment. Shared prerequisites live in
`tests/e2e/external/_support.py`: exit-77 gates, one absolute deadline per stage,
transient-tolerant polling, and JSON evidence. Fixes closed during the restructure: cleanup
corroborates every region capacity could launch in (policy default plus allowed plus stack
regions), readiness emits durable recovery evidence before terminal failure, connection waits fail
fast on `ActionRequired` with bounded post-stack revalidation for IAM visibility, the dead
`"deleted"` wire-status filter is gone, and cleanup automates the customer stack removal action
instead of failing on `ActionRequired`. Tailnet stages were repaired in the same pass: the workload
module no longer fails import inside the paid container (env defaults), the GPU workloads moved to
an import-clean module (the `httpx` driver import previously broke every GPU case in-container),
worker-status vocabulary uses `SchedulerWorkerStatus`, and tailnet cleanup is rerun-idempotent.
Tailnet device-set corroboration has no public surface and remains owned by the tailnet stages.

Security decision (2026-07-23): the user authorized creation of a same-account `default-test`
profile using the existing approved `default` bootstrap authority. The operator-profile stack
trusts that exact existing IAM user or role and creates only the limited operator role plus the
customer-stack execution role; it creates no IAM user or long-lived access key. `default-test` is
an AWS role profile backed by `default`, the control stack trusts only that exact operator role, and
Compose receives only the short-lived control-role session. Teardown order is operator stack, then
control stack only after customer cleanup proof.

Local prerequisite closure (2026-07-23): acceptance supports the installed AWS CLI v1 as well as
v2 and does not depend on the v2-only `aws configure export-credentials` command. Every stage
revalidates the exact assumed operator identity through STS; cleanup-window gating uses the
short-lived control-session expiration returned by activation because that is the credential
actually held by Compose and used to sign worker object capabilities. The operator profile itself
is refreshable and is never copied into the services. All live-stage modules still exit `77` when
their explicit authorization gate is absent.

Connected object-transfer decision (2026-07-23): remote workers never receive static object-store
credentials and never route large source, build-context, image-archive, or checkpoint bytes through
the public API/Funnel. The connected control stack owns one private, encrypted, public-blocked S3
bucket in the selected region and grants its exact control role only bucket metadata plus object
read/write/delete authority. Activation switches control-plane object storage from local Garage to
that bucket, uses the same short-lived role session including its session token, signs exact-object
HTTPS transfers with expirations below the remaining session lifetime, and keeps all logical object
purposes under distinct prefixes in that one physical bucket. Local-only Compose continues to use
Garage. The bucket and unfinished multipart uploads have explicit stack/lifecycle ownership; stack
deletion remains forbidden until public cleanup and an exact empty-bucket proof.

Image archives remain workspace-scoped: `(workspace_id, image_id)` is the durable identity and the
record points to the exact reserved object plus its real byte length and SHA-256. A build worker
computes those values before requesting its one-use upload descriptor; publication HEAD-verifies
the signed length and digest metadata before selecting the archive. Downloads are authorized
against the exact active assignment and carry the expected length/digest; the worker retries only
transient transfer failures, verifies the completed temporary file, fsyncs, then atomically
publishes it. Wrong worker/workspace/image, expired authorization, short reads, and digest mismatch
are terminal. Failed/unselected staging remains cleanup-owned and no Helm/Compose worker receives
archive S3 credentials.

Accepted repair contract (2026-07-23):

- PostgreSQL retains terminal provider records for billing, cleanup evidence, and diagnostics.
  The public current-instance list, active compute summary, and estimated cost count only
  nonterminal records; `deleted` and `failed` history never represents current capacity. Active
  records are ready, pending records are pending, and terminating records remain degraded and
  cost-bearing until cleanup proof completes.
- Pooled reconciliation holds the capacity-owner mutation lease, re-reads the durable generation,
  and terminalizes a provider record only after the provider proves its instance is absent or
  terminated and every recorded EBS volume is absent. Machine, worker, enrollment, and source-cache
  cleanup remain owned by the existing compute lifecycle; historical SQL rows are never deleted to
  fabricate zero.
- Raw `docker compose up` is not connected-AWS readiness. The deployment operator must validate the
  stack-owned non-root control role, obtain a short-lived session, and recreate only the
  credential-consuming services. The deployment entrypoint must be directly runnable and
  importable without a stdlib-shadowing filename or dynamic import.
- This repair changes no HTTP schema. Acceptance requires public pool desired/observed zero and
  ready, public current-instance total/cost zero, retained terminal SQL diagnostics, no open
  capacity operations for the owner, direct AWS EC2/EBS zero, an idempotent second scheduler cycle,
  and then the independent guarded preflight. Paid execution remains a later explicit stage.

Network transport contract (2026-07-23): task runners call the configured public HTTPS gateway
through the worker host's ordinary default-route egress. Tailnet owns only the reverse
gateway-to-agent/worker route. The worker network owner discovers the host's real IPv4/IPv6 egress
capabilities, reconciles complete bridge forwarding and masquerade rules, configures a namespace
default route for every enabled address family, and fails readiness when that production path is
unusable. OCI bundles use normal DNS and never pin the public gateway to a worker-resolved address.
`LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL` is the deployment/bootstrap authority; task
`GATEWAY_HTTP_URL` derives from it, while worker route transport/target remain independent. Local
acceptance is one real agent-bridge Function through the public CLI. The final guarded AWS stage
must prove a public runner callback, reverse Tailnet reachability, result/log durability, and exact
namespace, capacity, EC2/EBS, and Tailnet cleanup.

Network implementation evidence (2026-07-23): the Beta9 worker/agent workflow and tests were audited
for behavior before implementation. The worker now discovers the host's default IPv4/IPv6 egress
interfaces, serializes bridge/firewall and address allocation mutation, installs deterministic
forwarding and masquerade policy, prevents secondary-interface/Tailnet bypass of sandbox network
policy, and removes probe reservations and namespace state on every terminal path. OCI bundles use
the selected host resolver with an immutable worker-image fallback and no public-gateway DNS
pinning. Registration establishes the authenticated Pending worker session first, then the worker
proves a real child-namespace request to the public gateway `/health`, and only then transitions to
Available. Compose derives its public callback origin from the published control-plane port while
leaving the reverse Tailnet target independent.

Focused Ruff/format and BasedPyright passed with zero findings; the integrated worker/API batch
passed 23 tests with 26 expected external-service skips. The root Compose stack was activated with
its deployment-owned short-lived control role and all services were healthy. The public SDK
Function acceptance then built and durably published a 703 MiB Linux image archive through the
connected S3 capability, scheduled and ran the Function through the production worker/runner path,
returned `49`, and deleted the app. Cleanup proof found no active app row, no workload or readiness
network namespace beyond the separately owned `services` namespace, and no residual workload,
probe, or scenario-specific firewall rule. This closes the local network acceptance; the paid AWS
worker callback/reverse-route/EC2/EBS/Tailnet proof remains part of the guarded T-005 live stage.

Historical design below is retained only for the already-implemented public scale-zero,
concurrency, and cleanup invariants. Any IAM-user, `tools.*`, old `e2e/`, or bundled environment
command reference is superseded and must not be restored.

Source: the connected-AWS release startup exposed both repeated full-stack build cost and a
Compose-only IAM-user contract that diverges from production workload identity.

Superseded historical workflow (do not implement or run):

- One deployment-owned command exposes `setup`, `test`, `status`, and `teardown`. `setup` validates explicit
  AWS profile/account/region/spend/release/gateway inputs, idempotently provisions the test control
  role, refreshes a short-lived STS session, atomically activates Compose, and starts only affected
  services with `--no-build` by default. `test` creates or resumes the dedicated public connection,
  runs one T-005 workload, and reaches a stable zero-compute state without disconnecting. `status`
  reports the durable phase, exact stack/connection/workload identities, and any cost-bearing AWS
  resources. `teardown` removes the acceptance environment in exact ownership order.
- Success, failure, and interruption retain task/container/log evidence and the zero-cost workspace,
  connection, customer stack, control stack, and internal pool. They automatically delete only the
  active acceptance app, cancel pending work, and invoke the public idempotent
  `lazycloud pool scale <pool> --nodes 0` owner path before proving managed EC2/EBS/capacity zero.
  The command uses `PUT /api/v1/pools/{pool}/scale`; read-only setup/status use
  `lazycloud pool status <pool>` and `GET /api/v1/pools/{pool}/state` to prove the durable desired,
  observed, phase, and status projection without mutation. The compute service owns the durable desired
  count and provider mutation, the AWS pooled provider owns the ASG transition, and reconciliation
  owns observed instances/storage. Scale zero retains the pool, provider infrastructure, connection,
  customer stack, and history. A concurrent newer scale may supersede the request and must surface
  as non-zero returned desired capacity rather than fabricated cleanup success. If cost
  stabilization cannot be proven, the operation fails with the exact remaining resources and keeps
  recovery authority. Repeated setup/test/status/teardown reconstruct from PostgreSQL, Redis, public
  resources, CloudFormation, and tags; no local manifest or duplicate resume store is allowed.

State-machine closure (2026-07-22): the public route delegates to the gateway capacity owner, which
holds the capacity owner's renewable Redis mutation lease and refuses scale-down while reservations
or pending/running containers still own capacity. Under that lease, the compute owner re-reads SQL
and commits the desired count plus scheduler sizing state in one transaction before calling the
provider. Scale zero increments the pool generation, records phase `updating`, retires the previous
sizing operation by clearing its operation identity, start/retry/backoff/failure fields, sets target
units to zero, marks the initial target reached, and records the scale-down time. Provider failure
leaves desired zero durable with phase `degraded` so reconciliation retries toward zero; it never
restores the prior nonzero intent. Pooled-provider reconciliation takes the same lease, re-reads SQL
after acquiring it, and only then mutates AWS, preventing a stale desired-one snapshot from
recreating paid capacity. SQL generation remains the second persistence fence. Lease contention and
active capacity ownership return typed conflicts; missing pools and provider unavailability use the
central typed 404/503 mappings. Repeated scale zero skips AWS only when durable and observed state
already prove convergence; otherwise it repairs toward zero.

Idempotent zero is provider-verified: while still holding the owner lease, compute freshly describes
the provider before skipping a mutation. Stored SQL zero cannot by itself prove AWS zero. If the fresh
provider state has desired or observed capacity, the same guarded operation repairs it toward the
durable zero intent. A requested zero always runs the reservation/container ownership guard because
fresh provider drift can make it destructive even when stored desired and observed counts are zero.
Final setup/test/status acceptance requires every retained pool's public state projection to report
durable desired zero, observed zero, and ready phase in addition to direct AWS and cost evidence.
Scale zero is a capacity transition, not a permanent pool pause: a later authorized workload may
create a new reservation and scale the retained pool up again. The acceptance environment therefore
removes the app/deployment producer, cancels active work, and treats any raced reservation, pending
workload, or later nonzero durable/public/AWS observation as failed stabilization rather than fake
success. The zero guard conservatively refuses unassigned pending/running workspace containers when
their final pool ownership is not yet resolved.
To prevent an already-idle worker from receiving new work while zero is terminating provider
capacity, the gateway marks every owner worker unavailable under the same owner lease after the
active-work guard passes. Scheduler final dispatch for a provider-owned worker acquires that lease,
re-reads the worker, and requeues rather than assigning when it is no longer available or its owner
changed. Local and ownerless workers retain the existing fast path.
- `setup` may repair one legacy connectionless internal-pool projection only when the dedicated
  workspace ownership is exact and public plus AWS evidence independently proves no active work,
  pending capacity, EC2, EBS, or Tailnet agent. It must refuse paid, foreign, or ambiguous residue;
  valid connected zero-capacity pools remain stateful and are never deleted by setup/test.
- CloudFormation owns the control role and its least-privileged `sts:AssumeRole` permission. The
  authorized operator profile may assume it for local Compose; production supplies the same base
  role contract through workload identity. Customer CloudFormation owns its external-ID-protected
  connection role; PostgreSQL/Redis retain their existing production ownership.

Historical live evidence (2026-07-22): external Funnel health, dedicated workspace authority, customer
CloudFormation, image build/publication, source sync/mount, local runner execution, and exact AWS
placement submission all pass. The certifier recreates the Compose worker after control services and
requires its authenticated scheduler projection with sufficient build capacity. Stateful live setup
and read-only status now pass with a ready placement-capable connection, retained diagnostic history,
keyless certifier, no EC2/EBS/active work, and zero projected hourly cost. Focused Ruff, formatting,
typing, 53 safety checks, and the real local Function path pass.

Historical blocker: the live Function run proved worker enrollment and AWS scale-up, then Tailnet credential
issuance returned 503. Routine stabilization deleted the app and active work but only waited for
natural pool scale-down; the durable desired count and scheduler sizing operation remained one, so
the ASG replaced the failed instance. The public HTTP/CLI surface is implemented, but the gateway,
compute, scheduler-state, and reconciler owners must complete the state-machine closure above before
routine stabilization invokes it and live Tailnet diagnosis resumes. The exact acceptance ASG was
emergency-stabilized to desired zero with the local scheduler paused; do not resume the scheduler
until the canonical durable owner has recorded zero.

Current Tailnet acceptance decision (2026-07-23): whichever Tailnet the user selects or supplies for
the run is approved regardless of its account name. It remains shared external state: inspect before
mutation, preserve all unrelated users, devices, tags, grants, ACL rules, DNS/routes, OAuth clients,
and keys, and create or remove only exact LazyCloud-owned test resources. Never apply the
whole-policy `deploy/tailnet` module or perform broad teardown unless the user explicitly assigns
whole-policy ownership and a reviewed plan proves unrelated state is preserved. The configured OAuth
client currently authorizes the existing test tags `tag:upnext-agent` and
`tag:upnext-control-plane`, while acceptance defaults request `tag:lazycloud-agent` and
`tag:lazycloud-control-plane`. Align this test run with its already-authorized tags, prove tagged
exchange and gateway health before paid AWS capacity, and leave unrelated Tailnet state untouched.

## T-005 — Replace connected-AWS acceptance with the public workflow — F-15

Owner: root/t005_connected_aws_acceptance

Priority: active with the T-033 prepared environment and AWS warm-capacity owner.

Source: `findings.md` F-15 and the failed live runs of the former provider-environment harness.

Description: Delete the large connected-AWS workflow harness. Keep one portable Function scenario
that can target local, staging, or any accepted provider without provider logic. Split AWS
acceptance into independently callable account-connection, one-machine readiness, and cleanup
scenarios. The account-connection scenario takes the AWS account ID as its only product input and
automates only the customer CloudFormation authorization action with ambient customer credentials.
No scenario builds, deploys, migrates, activates Compose, owns release state, or repeatedly
inventories the platform.

Frozen workflow: one public account-ID connection stage; one public policy stage that selects AWS
and establishes the same one-worker warm headroom as the platform; one deterministic public Python
Function with no placement override; and one independent public cleanup stage with final
workspace-tagged AWS corroboration. PostgreSQL, Redis, the compute policy, provider adapter,
scheduler, agent, worker, and public Function owners retain their production authority. The
scenarios have no release publisher, Compose owner, private state, resume manifest, or fake
lifecycle.

Contract decision (2026-07-21): route, lease, and reusable-token rows are private implementation
state with no workspace-token projection. The live smoke must not gain administrator, database, or
Redis access solely to enumerate them, and the product must not expose private cleanup internals for
a test. Their residue invariants remain focused owner tests; live acceptance proves the public
durable owner boundary plus exact AWS and Tailnet absence.

Implementation checklist:

- [x] Delete the oversized connected-AWS Function/support harness and stale session/resume
  machinery.
- [x] Add one provider-neutral public Function round trip with result, log, app deletion, and
  restored-baseline evidence.
- [x] Add one AWS account-connection scenario driven by account ID and the public connection
  workflow.
- [x] Add one AWS machine-readiness canary and one independently callable public cleanup/leak
  audit; direct AWS reads occur only for customer authorization and final corroboration.
- [x] Verify the ambient customer identity matches the requested account, validate the exact
  CloudFormation action before execution, keep output secret-safe, and use exact external module
  invocation as the paid authorization without redundant live/paid switches.
- [x] Delete the old provider/environment harnesses and wrappers; document exact module commands in
  `tests/e2e/README.md`.
- [x] Run scoped Ruff, format, BasedPyright, and exact module help checks.
- [x] Keep Function cleanup and provider teardown independently callable; the Function returns its
  durable task ID, and the AWS cleanup stage can converge a retained connection without a local
  resume store.
- [x] Package the Function workload relative to its explicit source root so the same source archive
  is valid on local, platform, and customer workers.
- [ ] After the operator activates the release and prepares the warm AWS default, run one authorized
  live `Function → result/logs → delete → identical warm baseline` gate, then explicitly disable
  the AWS default and prove global zero before ending this acceptance session.

Acceptance commands:

```sh
uv run --group dev ruff check tests/e2e/external/aws
uv run --group dev ruff format --check tests/e2e/external/aws
uv run --group dev basedpyright tests/e2e/external/aws
# account_connection also requires the immutable release values it verifies the
# customer action against; without them the command exits on missing arguments.
uv run python -m tests.e2e.external.aws.account_connection --account-id <account-id> \
  --template-url "$LAZYCLOUD_AWS_CONNECTION_TEMPLATE_URL" \
  --platform-principal-arn "$LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN" \
  --execution-role-arn "$LAZYCLOUD_E2E_AWS_CUSTOMER_STACK_EXECUTION_ROLE_ARN"
uv run python -m tests.e2e.external.aws.one_machine_readiness
uv run python -m tests.e2e.function_round_trip --run-id <run-id>
uv run python -m tests.e2e.external.aws.cleanup --account-id <account-id> \
  --execution-role-arn "$LAZYCLOUD_E2E_AWS_CUSTOMER_STACK_EXECUTION_ROLE_ARN"
```

## T-036 — Make Helm bootstrap own administrator authority

Owner: root/helm_bootstrap_acceptance

Priority: active in the deployment-chart owner; required before Kubernetes worker certification.

Source: T-017 k3d acceptance reached a healthy control plane but the scheduler failed with
`workspace not found: default` because the chart owned schema migration and worker-token bootstrap,
not administrator/workspace creation.

Outcome: after the current schema is ready, one least-privileged chart-owned Job idempotently creates
the initial administrator/workspace authority and publishes the one-time credential only into an
explicitly owned namespace Secret. Scheduler startup and worker-token bootstrap wait for that durable
authority. Tokens never enter values, command arguments, logs, or generated host files; reinstall,
failure, and cleanup have one unambiguous owner.

Acceptance: focused Helm render/lint and chart contract tests pass, then one reusable k3d
installation proves bootstrap, worker replica mutation, real worker-Pod termination, replacement,
scale-down hysteresis, and namespace/release cleanup. The full AWS lifecycle is outside this task.

## T-037 — Make local worker acceptance thin and deterministic

Owner: root/fast_worker_acceptance

Priority: active in the E2E owner; replaces repeated isolated full-stack builds for ordinary worker
iteration.

Source: T-017 Compose acceptance works, but its 1,000-line isolated-project harness rebuilds and
recreates substantially more infrastructure than the worker boundary requires.

Outcome: one small scenario uses a healthy root Compose stack and public `lazycloud`/
`lazycloud-admin` surfaces to prove scale-ahead, a real workload, the authenticated production
interruption/drain transition, replacement, scale-down, and exact task-owned cleanup. It rebuilds or
restarts only explicitly requested affected services and contains no planned-command success,
duplicate infrastructure owner, mock lifecycle, or test-only product hook.

Acceptance: focused Ruff/type/import and destructive-safety checks pass; after the shared dirty tree
is integrated, run the scenario once against the root stack and prove no workload, reservation,
allocation, or task-owned worker residue. Helm and AWS provider certification are separate tasks.

## T-017 — Complete proactive capacity scaling and preemption handling

Owner: root/t017_capacity_scaling

Priority: implementation and local Compose behavior accepted; T-036 and T-033/T-005 own the
remaining Kubernetes and AWS certification boundaries.

Source: reopened worker placement and capacity-acquisition work after the Beta9 parity audit found
that placement-miss provisioning was complete but proactive pool sizing, pool failover, and the
preemption lifecycle were not.

Existing accepted baseline:

- Preserve deterministic greatest normalized post-placement CPU, memory, and GPU headroom, every
  hard eligibility filter, stable request order, canonical capacity-owner identity, durable pending
  capacity reservations and allocations, one-unit placement-miss acquisition, registration as
  success proof, bounded retry, cancellation, drain, provider cleanup, source-cache cleanup, and
  the already accepted public pool lifecycle.
- Preserve workload `min_containers`, keep-warm, endpoint demand, queue-depth autoscaling, and Pod
  desired-capacity reconciliation. This task does not add predictive traffic forecasting or
  priority-based eviction of one running workload for another; Beta9 does neither.

Public workflow and terminal outcomes:

- Pool create/update/list through the typed SDK, public `lazycloud` CLI, and `/api/v1` expose one
  canonical policy for initial/minimum/maximum workers, minimum free CPU millicores, memory MiB and
  GPU count, default worker shape, default eligibility, explicit pool priority, scaling state,
  idle-drain hysteresis, registration deadline, and whether the capacity is preemptible. Omitted
  headroom is zero; explicit zero disables that resource threshold without changing other floors.
- Applicable Function, Endpoint, TaskQueue, Pod, and Sandbox authoring surfaces expose one
  canonical `preemptible` opt-in. Non-preemptible work never uses preemptible workers. Explicit pool
  or capacity-owner placement remains strict and never silently falls back; an unpinned request may
  try the next compatible, healthy, default-eligible pool in stable priority order.
- Pool creation, scheduler restart, policy update, or worker loss reconciles `initial_workers` and
  `min_workers`. Thereafter the scheduler maintains configured free-resource headroom so compatible
  capacity can already be registering or ready before a placement miss. A threshold breach requests
  at most one bounded unit per owner reconciliation and never exceeds the current maximum or cost
  policy.
- Customer AWS reaches the same CPU startup baseline when it is the workspace default placement:
  one canonical default-region/default-instance-type pool starts and remains at one worker with at
  least 1,000 millicores and 1,024 MiB free. The typed AWS workspace policy owns that default
  instance type plus initial/minimum worker and free-headroom values; defaults match the managed
  Compose and Helm pool. Policy update creates or updates that one pool before workload demand, and
  control-plane startup restores it idempotently. Larger CPU shapes and all GPU-specific pools remain
  demand-owned at a zero floor, so historical shapes do not each retain paid capacity. Explicit AWS
  placement while Managed remains the workspace default does not silently purchase a permanent
  baseline.
- A provider or node interruption moves capacity through available, preempting, cordoned, draining,
  and terminated/replaced states. New work stops immediately; the preempting unit stops satisfying
  baseline or warm-headroom targets; replacement acquisition begins before loss when allowed; and
  grace-window completion or checkpointing is attempted without claiming that arbitrary Python
  execution can migrate in place.
- Work assigned but not started is requeued exactly once. Started work records a typed `preempted`
  outcome: TaskQueue and Function attempts retry only through their existing explicit retry and
  idempotency policy, Endpoint requests fail without blind replay, Pods restore desired capacity,
  and Sandboxes terminate with an inspectable preempted reason. Cancellation and timeout continue
  to win when they became authoritative first.

Ownership and state machine:

- PostgreSQL owns pool policy, priority, capacity-owner identity, provider instance/machine
  lifecycle, interruption state, retry intent, terminal reason, and durable audit. Redis owns hot
  worker state, owner locks, reservations, allocations, claims, leases, and wakeups. Neither store
  duplicates the other's authority.
- The scheduler owns effective free-capacity calculation, pool selection, baseline/headroom
  reconciliation, retry timing, and drain coordination. Effective headroom counts compatible
  Available plus unclaimed Pending capacity and subtracts active reservations and allocations;
  provisioning operations use the existing owner lock and stable operation identities so concurrent
  reconcilers and requests cannot double-provision.
- Provider controllers own create/status/delete for provisionable units. The AWS node agent reports
  IMDS spot interruption before shutdown; Kubernetes workers propagate the supported termination
  signal. Connected self-hosted agent pools consume eligible connected machine capacity but never
  fabricate physical capacity. Unsupported providers remain absent and fail explicitly.
- Pool health is operational, not advisory: degraded pools stop proactive acquisition, retain
  durable cooldown/retry state, and are skipped only for unpinned failover. At-limit and temporary
  unavailability try the next compatible default pool; explicit placement returns the exact bounded
  failure. Recovery resumes from durable state after scheduler or provider-controller restart.
- Scale-down and scale-ahead share one owner decision. Drain removes only capacity above worker
  floors and effective warm headroom after a cooldown/hysteresis window, never a worker with active
  containers or allocations, and cannot oscillate against the sizing reconciler. Deletion and
  interruption release exact reservations, allocations, provider resources, worker state, routes,
  credentials, cache ownership, and coordination keys while retaining bounded audit evidence.
- For the canonical customer-AWS pool, PostgreSQL owns the selected offer, desired/minimum one,
  headroom policy, instance/machine identity, and replacement state. The scheduler uses the existing
  sizing state machine and capacity-owner lock; the AWS provider owns ASG/EC2/EBS mutation; the
  enrolled worker reports actual schedulable capacity. Concurrent policy/startup reconciliation is
  idempotent. Failure leaves durable pool/sizing state for bounded retry, and connection removal
  drains the baseline through the existing provider cleanup state machine.

Frozen contracts and exclusions:

- Replace the internal `preemptable` spelling with canonical `preemptible` across SDK/CLI, HTTP,
  shared contracts, services, repositories, scheduler, worker, runner, agent, provider, Helm, and
  Compose latest-only; do not preserve aliases or dual wire fields.
- Pool ordering is an explicit typed policy field, not a label or capacity-owner UUID. Warm capacity
  is scoped by pool shape and preemptibility; preemptible headroom cannot satisfy a non-preemptible
  target. GPU headroom matches the pool's configured GPU type/count and cannot consume CPU-only or
  incompatible pending units.
- Do not add speculative demand prediction, arbitrary execution replay, running-workload victim
  selection, unsupported-cloud adapters, in-memory interruption truth, or a test-only provisioning
  path. LLM token-pressure autoscaling and deployment rollout-capacity preflight remain separate
  future capabilities.

Acceptance:

- Focused contract/repository/service tests prove omitted-versus-zero policy, priority ordering,
  available/pending/reserved/allocated arithmetic, initial/minimum restart reconciliation, one-unit
  threshold acquisition, maximum/cost bounds, pending deduplication, degraded and at-limit failover,
  strict pinned placement, cooldown/backoff, drain hysteresis, restart recovery, and concurrent
  reconciliation with one mutation winner.
- Preemption tests prove public SDK/CLI/API opt-in, non-preemptible isolation, authenticated and
  stale-safe interruption notice handling, immediate cordon, replacement-before-loss, grace-window
  completion, checkpoint opportunity, exact queued requeue, policy-bound started-work retry, typed
  Endpoint/Pod/Sandbox outcomes, cancellation races, provider failure, duplicate notices, restart,
  and exact cleanup without fabricated success.
- Run scoped Ruff/format, BasedPyright, and combined changed-owner tests after coherent slices. Then
  run the public `lazycloud` pool/deploy/invoke workflow and the production Compose capacity smoke
  through PostgreSQL, Redis, scheduler, worker, runner, object storage, provider lifecycle, results,
  logs/events, retries, scale-ahead, scale-down, interruption, and leak audit.
- Kubernetes acceptance proves replica mutation, pending-capacity accounting, termination handling,
  replacement, hysteresis, and cleanup through the supported Helm deployment. With explicit live
  authorization, the connected-AWS lifecycle proves real capacity creation, agent enrollment,
  schedulability, a real interruption notice or provider-supported equivalent, replacement, workload
  outcome, provider deletion, and exact platform/external cleanup. Missing live authorization is a
  recorded blocker, never replaced by a fake local success path.

Implementation evidence (2026-07-21):

- Implemented the frozen pool/placement/preemptibility contracts through SDK/CLI, HTTP, PostgreSQL,
  compute/provider controllers, scheduler, gateway, agent, worker, runner outcomes, Helm, Compose,
  and web inspection schemas. PostgreSQL revisioned CAS owns sizing state; Redis owns hot locks,
  reservations, allocations, claims, interruption cordon/requeue, and bounded observations.
- Focused public/compute/provider/database/API/scheduler/worker tests passed in coherent batches;
  the real-Redis scheduler batch passed 119 tests. Final scoped Ruff passed, full BasedPyright passed
  with zero errors, the final integrated subset passed 82 tests with 72 expected real-Redis skips,
  Helm template/lint and all 28 chart tests passed, and the canonical `preemptable` spelling is absent.
- Live isolated Compose acceptance passed with current images: two ready workers, concurrent public
  Function dispatch across both, bounded unassigned capacity failure, public app deletion, empty
  reservation/allocation scans, clean logs/restarts, and complete project resource cleanup.
- The Kubernetes smoke now requires proactive headroom before a second request, real worker-Pod
  SIGTERM, typed `PREEMPTED`, replacement registration, retry success, a 30-second no-drain window,
  eventual return to `minWorkers`, and exact cleanup. A live run found and closed the earlier Helm
  pre-install database-bootstrap ordering defect with a managed spec-hashed one-shot Job.

Release-audit blocker (2026-07-21): publication is paused until four invalid terminal/retry edges are
closed. Generic process `SIGTERM` is administrative shutdown, never provider-preemption authority;
only an authenticated, credential-generation-fenced interruption notice may emit `Preempted`.
Container termination and workload retry/requeue intent must commit as one durable PostgreSQL-owned
transition (or durable intent consumed by a restart-safe reconciler) before worker coordination state
is removed. Provider failures persist only bounded typed error codes/messages, never raw exception
text. An expired unused join credential rotates generation under the same idempotent capacity
operation, revokes the old authority, and preserves one current enrollment winner. Acceptance must
prove crash-after-container-terminal recovery, ordinary SIGTERM non-preemption, secret-shaped
provider-error redaction, and expired-credential retry before this row can return to release-ready.

Blocker: the supported Helm chart has no administrator-bootstrap owner. After schema bootstrap the
live k3d run reached a healthy control plane, but the scheduler failed with `workspace not found:
default`; worker-token bootstrap correctly withheld credentials because offline administrator
bootstrap had not established workspace/token authority. Closing this changes the deployment's
credential-publication and security contract. Prefer a chart-owned, least-privileged one-shot admin
bootstrap that publishes the one-time credential into an explicitly owned namespace Secret, with
worker-token bootstrap and scheduler capacity reconciliation gated on that durable authority. The
alternative is requiring an operator-provisioned existing workspace/admin Secret and failing Helm
validation before install. The temporary cluster was deleted. Connected-AWS interruption acceptance
also remains blocked on explicit live authorization; do not substitute a simulated provider event.

## T-014 — Make workspace container-shutdown eligibility authoritative

Owner: root/t014_workspace_shutdown

Source: retained cache-truth and trusted-execution workspace deletion investigation.

Description: Workspace deletion currently snapshots every historical container, including terminal
rows whose scheduler state is gone, dispatches stop events to their historical workers, and waits
30 seconds for impossible acknowledgements. Capture shutdown targets from PostgreSQL Pending and
Running rows only; terminal history is already complete and must never create stop delivery state.

Public contract and state machine:

- The existing public workspace DELETE remains bodyless `204`; deleting a workspace with terminal
  history returns without a worker wait. An actually active offline worker retains the current
  explicit failure outcome.
- Exited, Failed, and Stopped at the PostgreSQL capture point are complete/no-dispatch. Pending
  unassigned work completes through scheduler cancellation. Running assigned work targets the exact
  captured worker. A concurrent natural exit or durable cancellation proof completes the wait.
- Retry re-captures only rows still Pending or Running. Sibling workspaces and app lifecycle shutdown
  ownership are unchanged.

Ownership:

- PostgreSQL container state is authoritative for workspace shutdown eligibility. The orchestration
  repository owns the active query, operations management exposes the captured targets, and
  `WorkspaceDeletionService` coordinates stop and confirmation. `ContainerShutdownService` owns
  delivery and acknowledgement only for eligible targets.

Acceptance:

- Focused repository/service tests prove all three terminal statuses with stale worker IDs create no
  event/pending/ack state; only Pending/Running are captured; concurrent terminal completion works;
  active/offline still fails explicitly; siblings are unchanged.
- Rebuild the affected root Compose service and delete a workspace containing only terminal
  containers through the public/operator surface without the previous 30-second wait.

Implemented checkpoint: `f90e2dcd` makes the PostgreSQL Pending/Running query the sole shutdown
eligibility authority, leaves historical IDs only for scoped Redis cleanup, and preserves exact
assigned-worker acknowledgement, scheduler terminal/cancellation completion, and explicit offline
failure. Scoped Ruff/format/types passed; four focused local tests passed, and the agent's real-Redis
run passed five shutdown cases. Final public Compose acceptance is intentionally combined with the
fresh current-schema cycle after T-004 lands.

## T-004 — Make source-cache cleanup durable and restart-safe

Owner: root/t004_source_cache_cleanup

Source: workspace-deletion production failure; separate from the immediate T-014 eligibility bug.

Description: Replace synchronous worker acknowledgements during workspace deletion with
PostgreSQL-owned, generation-keyed cleanup intent. Workers reconcile pending cleanup for their
physical cache generation before becoming available; Redis remains advisory delivery only.

Public contract:

- Public workspace DELETE first durably moves Active to Deleting, revokes workspace credentials,
  snapshots source cleanup targets and fences new admission, then converges external cleanup and
  returns bodyless `204` only after the Deleted tombstone and one deletion audit event commit. It
  never waits for an offline worker solely for source-cache cleanup; T-014 still requires explicit
  termination proof for an active workload assigned to an offline worker.
- A failed/interrupted delete remains Deleting and an authorized retry resumes it; concurrent delete
  is serialized, and a repeated DELETE of the Deleted tombstone is idempotent `204`. Other public
  workspace operations require Active. Admin-only cleanup status reports bounded pending/claimed/
  completed counts, generations pending and oldest age without local paths or source contents. The
  public SDK does not expand.

Ownership and state machine:

- Relational `worker_cache_generations` identifies one physical cache by a non-secret UUID marker
  beside its root: same disk/process restart reuses it, replacement storage creates a new generation.
  States are initializing, available, draining and retired with a session fence and last-seen data.
- Relational cleanup targets are unique by workspace, generation and source object. They remain
  pending/claimed/completed with attempt count, retry time, lease token, bounded safe error code and
  timestamps; claims use `FOR UPDATE SKIP LOCKED`, expire safely, and resolve only for the exact
  generation/session/claim. No JSON payload is durable truth.
- The begin transaction locks the workspace, transitions Active to Deleting, revokes its credentials,
  snapshots every source object and non-retired generation, and inserts targets with conflict-do-
  nothing. External and ephemeral cleanup converges while the workspace remains fenced Deleting;
  the final transaction then purges remaining owned rows, writes Deleted plus the single audit event,
  and commits. Only the source-cache wake may be best effort after commit because PostgreSQL already
  owns that work. Dropped delivery, API/Redis restart, and failure between phases cannot reopen
  admission or lose cleanup work.
- Startup opens/creates the physical marker, registers initializing, drains exact claims, atomically
  activates only at zero pending, then begins normal work. Pending work fences an available worker
  draining/unavailable; keepalive cannot promote it. Generation/session identity accompanies worker
  availability and request-stream calls, and PostgreSQL availability is rechecked before every wait
  and dequeue so an already-open stream cannot outrun a new cleanup target. Local purge is idempotent
  and shares source-cache locking. Replacement generations cannot complete old work; retirement
  requires authoritative storage-destruction proof, never time alone.
- The API serializes each entire deletion attempt with a PostgreSQL session advisory fence. Tenant
  writers take the workspace key-share fence and require Active; cleanup owners use explicit system
  methods while Deleting. Active object-write claims block finalization until their authenticated
  stream completes. Workspace/source uploads use only the authenticated `/gateway/objects/stream`
  path: the presigned-create path is removed because an issued PUT cannot be revoked at deletion.
- Pending source-cache targets do not block the final Deleted tombstone or `204`; they remain durable
  and visible to the administrator status surface until the same storage reconciles or its lifecycle
  owner proves destruction. All other synchronous external/ephemeral cleanup must succeed first.

Acceptance:

- Fresh-schema and focused repository/service tests prove transaction rollback/idempotency,
  concurrent claims, lease expiry, retry, stale session/generation rejection, activation fencing,
  marker reuse/replacement, exact sibling-preserving purge, and cleanup-before-availability.
- Lost Redis wake plus API/Redis restart still reconciles; offline DELETE returns `204` with pending
  status and same-cache restart drains it before Available; a replacement cannot acknowledge old
  storage until authoritative retirement.
- One fresh root Compose cycle runs storage, cache-truth, trusted-execution and private-registry
  production acceptance, observes cleanup, and proves exact PostgreSQL/Redis/object/filesystem/
  credential/process removal with sibling preservation.

Persistence checkpoint: relational generation identity, session fencing, durable cleanup targets,
leased claims, retry/completion transitions, destruction-only retirement, bounded status aggregation,
and workspace-purge survival are implemented against the current PostgreSQL baseline. A cache
marker is permanently bound to one active storage identity and server-derived global/private
workspace scope; cleanup snapshots reach shared workers and only the deleted workspace's private
workers. Snapshot inserts are batched under shared generation locks, and failures persist only a
closed safe code. Focused Ruff/format and full BasedPyright pass; the final focused fresh-PostgreSQL
run passed nine tests. API deletion coordination, worker reconciliation, admin status, and
production acceptance remain.

Blocker: the Helm worker cache is currently a Pod-owned `emptyDir`, but Pod termination, deletion,
or `NotFound` does not prove kubelet erased node-local bytes after a hard-dead or force-deleted Pod.
The current Kubernetes provider owns Deployment scaling only and has no physical-volume lifecycle
identity; adding a Pod watcher would still be a false proof. Multiple agent worker slots also share
one cache root while independently claiming generation ownership. Closing this honestly changes
the cache owner and infrastructure/cost contract: prefer one node/machine-owned cache service with
one generation per physical root and provider-backed disk-destruction proof; the alternative is one
CSI-backed ephemeral PVC per worker and retirement only after backend volume deletion. Do not add a
timeout, heartbeat, replacement-Pod, Redis-TTL, or Pod-absence retirement path. Await product
direction before changing this ownership or running final T-004 production acceptance.

## T-013 — Persist the assigned workload image in scheduler container state

Owner: root/t013_scheduler_image_assignment

Source: production failure exposed by T-009 cache-truth live acceptance.

Description: Normal workload scheduling leaves `SchedulerContainerState.image_id` empty because
`_container_state()` copies the payload image only for image-build requests. The worker requests the
real assigned archive and the control plane correctly rejects the mismatch. Persist the immutable
image assignment for every image-backed request without weakening origin authorization.

Public contract:

- Public deploy/run behavior is unchanged except that a legitimately assigned normal workload can
  now download its exact image archive and reach user code after a cache miss.
- Missing or mismatched worker, container, workspace, stub, or image identity remains denied with no
  credential or archive disclosure. A retry observes the same immutable assignment.

Ownership and state machine:

- The scheduler request payload remains the image-assignment source; `SchedulerContainerState` is
  the durable coordination projection used by worker-repository authorization. `_container_state()`
  copies `payload.image_id` for normal and build work.
- Build ID and upload capability remain image-build-only. Pending, assigned, failed, and retried
  state retains the same image ID; cancellation/deletion removes the state through current owners.
- No fallback, inference from worker input, or relaxed authorization is permitted.

Acceptance:

- Focused scheduler tests prove normal and image-build state projection plus retry/failure retention;
  worker-repository tests continue to reject every mismatched identity dimension.
- Scheduler Ruff/format/type checks and the relevant integration tests pass.
- Rebuild the affected root Compose services, then run
  `uv run python -m tests.e2e.local.cache.scenario_restart --live`. It must invoke through the public
  Function path before and after a cache-server restart and clean its exact app through the public
  owner. Cache miss, corruption, fencing, and private-store transitions remain cache/worker owner
  tests.

Implemented: `830d1c8f` persists the normal workload image assignment. The focused Redis-backed
scheduler suite passed 58 tests, worker-repository authorization passed 12 with two environment
skips, the rebuilt services are healthy, and cache-truth now completes its user/cache workflow
without the former load-image denial.

Blocker: final cache-truth cleanup first reaches T-014: terminal historical containers are being
dispatched to a worker and waited on after their scheduler assignments are gone. T-004 follows for
durable cache reconciliation. Keep T-013 open until both remove the retained workspace and exact
scoped residue.

## T-012 — Make encrypted workspace secrets relational and latest-only — F-20

Owner: root/t012_relational_secrets

Source: `findings.md` F-20; post-device aggregate remeasurement.

Description: Make encrypted workspace-secret columns the sole PostgreSQL truth, remove generic
payload mapping and lazy plaintext compatibility, and make service mutations authoritative under
concurrency while preserving the public SDK, CLI, HTTP, and runtime injection workflows.

Public contract:

- Existing `Secret.create/set/get/update/delete`, `lazycloud secret` CRUD/list/show/masking/reveal,
  `/api/v1/secrets`, and named runtime injection behavior remain stable.
- Create on an existing workspace/name conflicts; update/delete on a missing secret return not found;
  set remains an explicit upsert whose last committed value wins while preserving ID/creation time.
- Plaintext values exist only at the public/service boundary and in the target workload environment;
  storage and diagnostics never reveal them.

Ownership and state machine:

- PostgreSQL `workspace_secrets` owns encrypted records. `SecretService` owns encryption, domain
  outcomes, events, and workspace-change publication; `SecretRepository` only maps and executes
  relational, workspace-scoped, atomic SQL. No Redis/object-store/filesystem state is owned.
- Missing transitions to encrypted current on create/set. Current transitions atomically on
  update/set and to missing on delete. Reads/lists decrypt without persistence writes.
- AES-GCM/HKDF retains workspace ID plus secret name as authenticated data. Unprefixed plaintext,
  malformed base64, tampering, wrong workspace, or rename fails closed; no lazy migration or
  compatibility fallback remains. Workspace deletion cascades rows.
- Frozen columns are ID, workspace, name, ciphertext, and timestamps. Flexible payload is absent;
  the current baseline changes directly with no migration ladder.

Acceptance:

- Fresh schema/mapper/repository tests prove no payload, direct relational truth, unique isolation,
  concurrent create conflict, atomic update/set/delete, cascade, and stable timestamps/IDs.
- Crypto/service/API/SDK/CLI tests replace plaintext migration with fail-closed malformed, tampered,
  wrong-workspace, and renamed ciphertext coverage plus exact conflict/not-found behavior.
- Run the independently selected storage scenarios under `tests/e2e/local/storage/` for Volume CLI,
  transfer, worker mount, CloudBucket mount, and Output behavior. Secret masking, encryption,
  isolation, relational persistence, and cascade invariants remain focused identity/database/storage
  owner tests; E2E does not inspect private PostgreSQL, Redis, JuiceFS, or object-store state.
- Run scoped Ruff/format, BasedPyright, combined owner tests, fresh PostgreSQL schema, then live
  storage acceptance. Do not create a separate smoke or preserve plaintext compatibility.

Implemented checkpoint: `83c14ff9` makes encrypted relational columns the only secret persistence
truth, gives create/update/delete exact atomic outcomes, gives set one database upsert path with
stable identity and monotonic timestamps, removes plaintext fallback and the obsolete planning
wrapper, and preserves the public SDK/CLI/API/runtime contracts. Scoped Ruff/format, full
BasedPyright, 188 combined owner tests, and a dedicated fresh-PostgreSQL schema/concurrency run pass;
root review repeated 24 focused tests with one environment skip.

Remaining acceptance: rebuild from the new latest-only baseline and run the production storage
workflow. Preserve the captured T-004 deletion evidence until its corrected terminal-container and
durable source-cache contracts are frozen; a schema reset is predeployment setup, not cleanup proof.
