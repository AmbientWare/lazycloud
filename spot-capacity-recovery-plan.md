# Spot capacity recovery implementation plan

Status: implemented on `feat/spot-capacity-recovery`. See
[acceptance evidence](spot-capacity-recovery-acceptance.md) for completed checks
and remaining live verification.
Code baseline: `main` at `02380bddc4c2918ff75e0f99456c9e030d42896e`.

## Outcome and scope

LazyCloud should respond to every threatened worker by securing compatible
capacity in another eligible market, preserving running work until its deadline,
and recovering interrupted work under an explicit retry policy. The CLI and
dashboard must explain whether work is queued, acquiring compute, starting, or
executing. Multiple notices must not produce duplicate purchases or exceed the
existing fleet limits.

Keep the configured providers, purchase markets, margin policy, workspace
boundaries, and CPU/GPU limits. This change does not enable another provider,
automatically buy On-Demand capacity for Spot work, or promise uninterrupted
service from a Spot-only fleet. The platform compute ownership/settings issue is
a separate change.

Warm diversification applies to the platform fleet. Recovery of customer-owned
cloud machines remains within that customer's authorized providers, placement
constraints, and account. Self-hosted machines cannot authorize provider purchases.

## Baseline behavior

| Owner | Current behavior and change required |
| --- | --- |
| [AWS interruption monitor](packages/providers/aws/src/provider_aws/provider_node_interruption.py) | Polls `spot/instance-action`. Add the separate rebalance recommendation signal. |
| [Agent daemon](apps/agent/src/agent_app/daemon.py) and [shutdown timer](packages/agent/src/agent/capacity_shutdown.py) | A notice with a deadline drains; a notice without one preempts immediately. Preserve the independent shutdown timer and distinguish risk from immediate loss. |
| [Gateway service](packages/gateway/src/gateway/service.py), [gateway contracts](packages/shared/src/shared/http/gateway.py), and [capacity state](packages/shared/src/shared/compute_enrollment.py) | Authenticate and persist notices, close admission, and notify scheduler preemption. Extend this path to record replacement demand durably. |
| [Scheduler preemption](packages/scheduler/src/scheduler/preemption.py) and [workload draining](packages/scheduler/src/scheduler/worker_rollout.py) | Requeue recoverable requests, drain workers, and prepare function/endpoint replacements. Preserve assignment fencing and workload-specific settlement. |
| [Pool drain](packages/scheduler/src/scheduler/pool_drain.py) | `_reconcile_replacement` handles both template upgrades and interruptions using one same-unit replacement pair. Separate urgent interruption recovery from serial planned upgrades. |
| [Fleet policy](packages/compute/src/compute/fleet_policy.py) and [compute service](packages/compute/src/compute/service.py) | `_reconcile_warm_market` selects one candidate and returns; `plan_warm_capacity` permits one maintenance surge. Plan warm floors across multiple units and account for all urgent replacements. |
| [Compute persistence](packages/database/src/database/repositories/compute.py) and [tables](packages/database/src/database/tables/compute.py) | Existing durable capacity operations, platform admission lock, terminal ownership fence, and retirement accounting are the purchase authority. Reuse them. |
| [AWS pooled provider](packages/providers/aws/src/provider_aws/pooled_provider.py) and [managed pool](packages/providers/aws/src/provider_aws/managed_pool.py) | Named release scales the group before terminating a named instance without decrementing desired capacity again. Cross-market recovery must adjust source intent before release and prevent autonomous replenishment in the rejected market. |
| [Image submission](packages/images/src/images/submission.py), [build service](packages/images/src/images/service.py), and [session planning](packages/images/src/images/building/lifecycle.py) | Dispatch retries exist, but stale active builds fail; the build ID also identifies the execution container. Add fenced execution attempts before restarting an interrupted build. |
| [SDK image operation](packages/lazycloud/src/lazycloud/abstractions/image.py) and [image client](packages/lazycloud/src/lazycloud/clients/image/control.py) | The terminal step starts as `preparing`. Expose durable scheduling progress alongside real build logs. |

The September 16 incident already demonstrated notice detection and attempted
replacement. A replacement surge was requested before the termination deadline,
but acquisition was rejected and usable capacity arrived later. This plan fixes
replacement breadth, recovery, and visibility. It does not assume missing notice
detection was the cause.

## 1. Represent risk and termination separately

Extend the AWS monitor to poll both interruption metadata and
`events/recommendations/rebalance` through its existing IMDSv2 transport. Preserve
token refresh, bounded requests, and explicit malformed-response errors. Poll
interruption first so an error reading recommendations cannot mask a deadline.
Neither a missing recommendation nor a later observation clears an existing warning.

Add a provider-neutral `AtRisk` capacity state and a typed signal kind. A rebalance
recommendation records its observation time without inventing a termination time.
Keep `notice_at`'s existing deadline meaning for actual interruptions. Do not route
an advisory through the current deadline-free immediate-preemption branch.

Required transitions:

- `Available -> AtRisk`: persist the signal, stop new container placement, request
  replacements, and keep the worker runtime and active work alive. Do not arm the
  shutdown timer.
- `Available/AtRisk -> Draining`: a real future interruption deadline arrives.
  Arm the timer and preserve the earliest deadline seen.
- `AtRisk -> Draining` for an application-directed handoff: replacements are
  serving, then use the existing planned drain and retire the old worker after
  its work finishes. An advisory alone never authorizes a forced stop.
- `Draining -> Preempting/Cordoned`: enforce the real deadline using existing
  graceful-stop and safety margins. Immediate provider loss retains its current
  immediate-preemption semantics.
- Duplicate or older reports cannot reopen admission, postpone a deadline, or
  create a second replacement obligation.

Update every `AgentCapacityState` consumer together: agent persisted state and
restart behavior, gateway request/response models, enrollment repositories,
scheduler interruption source, worker readiness, release reconciliation, and web
compute schemas. `internal_unit_interrupted_machines` currently requires a
non-null deadline and must become a typed recovery-candidate query that includes
advisories without treating them as expired deadlines.

Authenticate reports with existing enrollment/session and credential-generation
checks. Rotating credentials must not create another replacement for the same
physical machine. Continue processing a real deadline if the gateway is unavailable.

AWS recommendations can arrive early, alongside the interruption warning, or not
at all. They improve lead time but cannot be the only recovery trigger.
[AWS rebalance recommendations](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/rebalance-recommendations.html)

## 2. Give compute one durable replacement workflow

Add a compute-owned replacement service, composed by the API and scheduler rather
than implemented in either process. The gateway records recovery intent after an
authenticated notice; provider calls happen in the capacity worker, outside the
request and outside database transactions. Persist notice and intent atomically
where their ownership permits; otherwise reconcile the durable notice into intent
idempotently so a crash between them cannot lose recovery.

Use a new relational replacement-intent record with source unit, machine and
enrollment identity, signal/deadline, required resource shape, lifecycle state,
active acquisition operation, resulting machine, next action time, and revision.
Enforce at most one live intent per source machine. Keep intent identity stable
when an advisory becomes a termination notice.

Link acquisition attempts to the existing `compute_capacity_operations` records.
The intent describes why capacity is needed; those operations remain the sole
authority for purchase ownership, fulfillment, release, and provider side effects.
Do not create another purchase ledger or reopen terminal operations for retries.
An unsuccessful attempt gets a new operation only after accounting for any
uncertain resources from the earlier attempt.

Use the existing PostgreSQL platform capacity lock to reserve headroom before
provider calls. Preserve per-unit mutation leases and adopt a documented, uniform
lock order. A worker may process several independent admitted attempts in a bounded
batch; do not hold a global lock across network calls.

Accounting must distinguish:

- Serving capacity eligible for new work.
- Threatened capacity still executing and still billable.
- Admitted acquisitions that have not registered.
- Retiring or uncertain provider resources whose absence is not yet confirmed.

All billable commitments count toward fleet limits. A two-worker fleet losing
both workers may admit two replacements when the configured cap has room for
four. If the cap has no room, use other healthy capacity or report the limit;
never silently bypass it. Provider-autonomous launches must remain visible in
commitment accounting.

Give urgent recovery precedence over new planned upgrades and elective warm-pool
moves. Already-started maintenance still owns its resources. Release the current
single-maintenance restriction for urgent intents, while retaining shared purchase
admission. Keep the serial `replacement_machine_id` pair for template upgrades;
remove interruption selection from that path so the two controllers cannot replace
the same machine.

Warm-floor planning, request-driven acquisition, and interruption recovery must
credit the same admitted and serving capacity. A warning on one machine must not
independently buy a warm-floor replacement, a workload replacement, and an
interruption replacement. Workload demand may adopt compatible existing capacity
through the established reservation owner.

Before moving a source's capacity obligation to another unit, persist the source
desired-count adjustment and reconcile its provider launch behavior. For AWS,
protect retained instances from scale-in, lower the source group's desired count
to the retained target, and suppress launches while an affected market is closed
to acquisition. Preserve serving instances until work drains or the provider
deadline arrives. Use the existing named release sequence so desired capacity is
not decremented twice. Resume launches only when a new admitted acquisition makes
the market eligible again. Keep this lifecycle behind the pooled provider
protocol, with durable intent to recover partial failure. Do not enable AWS
Capacity Rebalancing as a second independent purchasing controller. Include any
already-running autonomous launch in commitments and named cleanup.

## 3. Diversify warm capacity and choose replacement markets

Use the typed fields already present on `ComputeOffer`: cloud/provider scope,
region, Availability Zone, instance type, architecture, and purchase market. Define
an explicit provider-neutral capacity-market identity; AWS maps it to the actual
instance type and stable AZ ID. Do not parse `capability_key` strings or mistake
LazyCloud's routing pool name for an AWS Spot pool. Separate provider authorization
scope from correlated market identity so two connections to the same AWS market
do not appear independent.

Replace the single-target warm plan with a plan of floors across eligible units.
For the current two-worker baseline, prefer two distinct markets and different
zones when compatible approved offers are available. Preserve a healthy diverse
placement rather than moving workers for small price changes. If only one eligible
market is available, retain usable capacity and emit a reduced-diversity event.
Do not drop the warm floor merely to satisfy diversity.

For replacements, apply existing hard placement and purchase admission first:
workspace/account, allowed provider and region/zone, runtime, architecture, CPU,
memory, GPU model/count, storage, data locality, supplier price completeness, and
margin. A worker-local filesystem or explicitly pinned worker cannot be relocated
by relaxing its binding. Preserve the configured GPU warm policy; replacement
shape handling must still support GPU workers.

Within eligible offers, prefer unaffected markets with available capacity and
less fleet concentration, then use existing offer selection criteria. Do not buy
again from the affected market during recovery cooldown. Record interruption risk
and typed acquisition failures with an expiry on the existing market/unit state.
Evolve `_failed_market_retry_ready` rather than creating a second failure cache.
Separate retry eligibility from the source unit's desired count and deletion:
source cleanup must not delay trying a different market.

Definitive capacity rejection immediately makes another candidate due. Quota,
authorization, and provider-wide failures retain their broader scope; rotating
through instance types is not a remedy for them. Unknown launch outcomes retain
their commitments until provider reconciliation proves what exists. Bounded
attempts and existing overall deadlines prevent indefinite acquisition loops.

Warm-floor transfers remain conservative: count only verified intake as serving,
retain healthy source capacity until the destination serves, and release floor
and ownership exactly once. Interruption recovery can run concurrently for every
threatened source admitted by the limit.

This follows AWS's recommendation to allow multiple compatible instance types
and Availability Zones without changing the configured provider strategy.
[AWS Spot best practices](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-best-practices.html)

## 4. Wake recovery promptly without increasing idle database cost

After durable intent commits, publish a Redis wake-up for the capacity worker.
Use the existing coordination and lease owners. A missed publication is recovered
by a bounded due-intent query; Redis is not the only record of work.

The current scheduler defaults include a 30-second pool-drain interval and a
60-second managed-compute interval. Urgent intent must bypass those scheduling
delays without reducing every loop's interval. Prioritize real deadlines over
advisories, and admit all due threatened sources in a pass within available limits.

Query active/due intents with indexed predicates and a batch limit. Fetch shared
fleet commitments, eligible market health, and worker readiness once per pass.
Do not scan terminal replacement history or fetch JSON event histories. Maintain
source/attempt state across scheduler restarts and Redis loss. A successful EC2
launch or agent enrollment alone cannot fulfill recovery: require compatible
worker release admission and current request intake.

Record timestamps for signal receipt, intent admission, acquisition request,
provider launch, usable worker, work reassignment, and source absence. Record
typed failure reason, selected market, and blocked headroom. Metrics should use
bounded dimensions; machine and operation IDs belong in structured events.

## 5. Recover work through its existing owner

Keep `SchedulerCapacityInterruptionService` responsible for worker admission and
preemption, and `WorkerWorkloadDrainService` responsible for preparing function
and endpoint capacity. Compute owns machines, not invocation retry policy.

- Unassigned or proven-undelivered requests retain their durable request ID,
  original deadline, and cancellation state when requeued. Redis loss alone is
  not proof that a request was undelivered.
- Running functions may finish during the notice window. At forced loss, use
  existing invocation claim settlement and retry budgets. Preserve cancellation
  and terminal results. No claim of exactly-once external side effects.
- Endpoints/ASGI prepare serving replacements and update routes through the
  existing rollout owner. Drain current connections where time permits; do not
  mark an unready replacement healthy to conceal a gap.
- Pods, sandboxes, and builds tied to a live filesystem retain their existing
  durability and placement restrictions. Do not invent transparent memory,
  connection, or local-disk migration. Report an actionable terminal outcome if
  the workload cannot recover under its supported contract.

Image builds need a distinct owner change. Add a durable build-attempt identity
and an execution container ID separate from the public build ID. Carry it through
dispatch, credentials, progress, result reporting, publication, and cleanup.
Update `fail_container_build`, which currently finds a build by container ID,
to resolve the attempt through a scoped repository lookup.

The proposed policy is one automatic infrastructure restart after confirmed Spot
loss for a build whose inputs remain available. Build-command failures and an
unexplained progress-lease timeout do not qualify. Keep the same public build ID,
preserve the operation deadline, and show the restart. Build commands may execute
again; this is not checkpoint resume. A build requiring a vanished source
filesystem fails with that reason.

Fence publication and every worker report by the active attempt and assignment.
An old worker cannot complete the new attempt, renew its progress lease, overwrite
its artifact, or resurrect a cancelled build. Give artifacts attempt-scoped upload
ownership and publish the winning result once. Remint authorized ephemeral inputs
for a new attempt when possible; never copy expired capabilities into it. If
required private inputs cannot be recovered, fail explicitly. Cleanup addresses
the retired attempt's container and artifacts without deleting a successful
successor or shared image archive.

## 6. Expose real progress through existing contracts

Use the existing task pending-progress vocabulary where it applies, including
`capacity_unavailable`, `capacity_limit`, `provisioning_compute`, and
`starting_container`. Derive build scheduling progress from the current attempt's
durable scheduling state. Do not call “request accepted” proof that image commands
are running.

Extend image response/event contracts with typed progress and attempt information
owned in `packages/shared`. Keep build execution phases distinct from acquisition
progress. Update these consumers in the same slice:

- [Image records](packages/shared/src/shared/image_building/records.py),
  [image HTTP contracts](packages/shared/src/shared/http/images.py), and
  [operation responses](packages/shared/src/shared/http/operations.py).
- `packages/images` persistence and event streaming, API image composition, and
  worker repository progress/result contracts.
- `packages/lazycloud` image client and `ImageBuildOperation` rendering.
- `apps/web` API schemas and the views that consume image/task/compute progress.

Render waiting for compute, starting a worker/container, building, and retrying
after interruption from those facts. Preserve machine-readable CLI output. Status
changes are status events, not fabricated build logs. Reconnecting clients replay
events by cursor and receive the authoritative terminal result even when the last
event was missed. Workspace authorization and sanitization apply to every event;
customer responses do not expose platform account credentials or internal errors.

## 7. Schema and delivery sequence

Implement in dependency order on feature branches, each with a coherent owner
slice and a PR. All listed slices are required for the complete outcome.

1. Add typed risk/replacement contracts, relational intent and operation links,
   indexed due queries, and compute admission/recovery decisions. Add migrations
   after the actual Alembic head at implementation time. Never modify
   `0001_relational_baseline` or reset production data.
2. Implement fleet diversification and market selection. Route actual
   interruption recovery through the new compute owner and remove its old
   same-unit surge path. Preserve planned upgrades and customer ownership.
3. Connect durable wake-ups, reconciliation, agent advisory detection, and
   workload handoff. Update every reader before new agents emit `AtRisk`.
4. Add fenced image attempts and bounded interruption retry, then public progress
   projection and CLI/web consumption. Existing active builds become attempt one
   with their original container IDs and timestamps; historical IDs stay valid.
5. Execute integrated acceptance, deploy through the repository release workflow,
   and verify production behavior and query rate.

Migrations must preserve active capacity operations, fulfilled ownership, pending
cleanup, and the current planned-replacement pairs. Reconcile an existing
interruption pair into one recovery intent without admitting a duplicate surge.
Completed historical builds need no eager attempt-history rewrite; resolve their
existing terminal result directly.

Use migrations first, then compatible backend readers and consumers, then agent
artifacts that produce the new signal. Drain old backend writers before enabling
the new processing through normal deployment. Do not add a feature flag, alternate
backend, or compatibility wrapper. Rollback must keep readers capable of parsing
persisted new states and servicing cleanup; prefer a forward repair once those
states exist. No destructive down-migration of live recovery state.

Update owning `AGENTS.md` descriptions of serial warm handoff and maintenance to
describe the final rules. Preserve their `CLAUDE.md` symlinks. Public documentation
changes describe shipped retry and progress behavior after acceptance.

## 8. Acceptance and evidence

Use existing owner evidence first. Extend only the smallest cases that prove a
new material invariant; do not add tests for orchestration, copy, or call order.

| Boundary | Required proof and existing starting point |
| --- | --- |
| Signals and agent lifetime | Advisory keeps runtime alive; an actual deadline wins; restart and duplicate reports retain the earliest deadline. Extend `packages/providers/aws/tests/test_provider_aws_node_interruption.py`, `packages/agent/tests/test_capacity_shutdown.py`, and `apps/agent/tests/test_agent_daemon_readiness.py` only where their owners cover the behavior. |
| Durable admission | Two schedulers processing two warnings create exactly two admitted obligations within the cap; duplicates, credential rotation, crashes, and late provider responses cannot duplicate ownership. Use real PostgreSQL at `packages/database/tests/test_platform_capacity_repository.py` and the capacity operation owner. |
| Selection and floor | Two warm workers diversify when eligible offers exist; two simultaneous losses can acquire elsewhere; a rejected market advances; limits and placement remain binding. Extend `packages/compute/tests/test_pooled_capacity.py` and `test_offer_selection.py`. |
| Drain and handoff | Serving readiness precedes planned retirement; queued work retains fences; active work and cancellation settle once. Adapt `packages/scheduler/tests/test_scheduler_preemption.py` and `test_scheduler_pool_drain.py`, removing interruption tests whose old same-unit mechanism is replaced. |
| Build recovery | Confirmed interruption retries once, old results cannot publish, unavailable inputs fail, cancellation wins, and cleanup preserves the successor. Start with `packages/database/tests/test_image_build_dispatch.py`, `packages/images/tests/test_image_build_container_scheduling.py`, and the worker image-build owner. |
| Public progress | One real `lazycloud run` shows acquisition, execution, optional interruption retry, and a terminal result, including reconnection after missed completion. Use the public client and real backend. |

Run narrow owner tests directly with `uv run pytest -x <changed-owner-files>`;
run Ruff and the repository's changed-scope type checks for implementation files.
Use Bun for any changed web schema/type checks. No broad release claim from a
narrow owner run.

Add one named live scenario, proposed as
`tests.e2e.external.aws.spot_capacity_recovery`, executed as a module. Use the
production provider adapter, agent, scheduler, worker, PostgreSQL, Redis, and
public CLI. Provision and identify a small dedicated fleet under the authorized
AWS `default` profile. Preserve shared account resources. Reuse healthy supporting
infrastructure; never interrupt unrelated production workers for acceptance.

Deliver actual AWS interruption notices to scenario-owned Spot instances using
the provider's supported interruption experiment. Scope every target to exact
instance IDs and verify experiment permissions and cost before execution. Exercise
two notices together while a function and reproducible image build are active.
Use owner-level authoritative evidence for deterministic acquisition rejection;
a live run cannot guarantee AWS happens to reject a market. Do not add production
fault flags or fake successful provider adapters to force it.

Poll and print independent signals every few seconds: notices at the agent,
durable intents and attempts, scheduler decisions, AWS instance/ASG state, worker
intake, workload state, and CLI progress. Investigate missing progress after a
cycle or two. Record notice-to-request and notice-to-serving latency separately;
external capacity availability prevents a universal two-minute serving guarantee.
Acceptance requires prompt admission independent of the old 30/60-second cadence,
correct recovery when capacity is obtainable, and truthful bounded failure when
it is not.

After the run, prove all scenario-owned instances, groups, experiment resources,
pending purchases, and temporary artifacts are cleaned up. Confirm sibling
resources are unchanged and late provider launches cannot leak after cancellation.

Before and after changing reconciliation, measure query counts and returned bytes
through the production owner with substantial terminal history and few active
resources. Cover idle, two simultaneous notices, rejected acquisition, restart,
and cleanup. Report totals across the configured replica count and cadence.
Idle work must remain bounded independently of terminal history; repeated durable
reads by a losing replica still count. Verify the deployed rate in query insights.

## Completion criteria

- Warm capacity prefers distinct approved markets without sacrificing hard
  workload requirements or silently changing spending policy.
- Every threatened machine has one durable recovery obligation, including when
  both warm workers are interrupted together.
- Replacement attempts can proceed in other markets before source termination,
  within existing caps and without a planned upgrade blocking all recovery.
- Work reaches a correct terminal result or an explicit, bounded failure;
  late workers cannot overwrite successor results.
- CLI and dashboard distinguish capacity delay from image execution.
- Database cost, live acceptance, cleanup, and deployment evidence are recorded.

The main cost tradeoff is temporary overlap between threatened and replacement
machines, bounded by existing limits. Diversity and earlier action reduce outages;
a guaranteed warm On-Demand baseline would be a separate product and cost decision.
