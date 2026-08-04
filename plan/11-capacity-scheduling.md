# Capacity, Scheduling and Worker Lifecycle

## Target state

One authority answers "this machine can run a container": the **scheduler worker
record** (`SchedulerWorkerRecord` in Redis) with `status is Available` and free
capacity greater than the request. Nothing else may be consulted for that
question, and nothing else may claim it.

Everything currently competing for that answer becomes either an input or a
derived view of it:

- `enrollment.readiness_phase` stays what it already is — a statement about the
  **agent**, not about capacity — and stops being rendered to users as "Ready".
- The durable `Worker` row stops asserting `Running` at registration time; it
  becomes inventory (identity, machine binding, last-seen), never readiness.
- `MachineBootstrapPhase` becomes strictly what the node reported, with no
  `Ready` member. The user-facing verdict is a separate derived type computed by
  one shared function that reads the scheduler worker record.
- "How many machines do we want" collapses from six durable representations to
  two: the provider's own desired count, and one `compute_capacity_operations`
  row per in-flight unit.
- A capacity reservation becomes a **claim** — owner, shape, containers,
  deadline, optional target worker — not a six-state machine with its own lock,
  CAS and frozen transition graph.
- Being at the configured maximum is backpressure, not failure, and never marks
  a pool degraded.
- Every transition to `Unavailable` carries a typed reason and is durable.

## Why this is simpler and more reliable

The live outage was a `ValueError` from a value the system creates itself.
`reserve_pending` persists a reservation in `Provisioning` with the model default
`desired_unit=0` (`packages/scheduler/src/scheduler/capacity_reservations.py:1174-1180`,
default at `:162`). `reconcile` guards that case
(`:1538-1540`); `_acquire_from_controller` does not (`:1266-1276`), so it reaches
`ensure_acquisition`, which raises (`:592-600`). Compute cannot produce a zero —
`_capacity_planning_result` clamps with `max(desired_unit, 1)`
(`packages/compute/src/compute/service.py:5479`), the contract is `Field(ge=1)`
(`packages/shared/src/shared/capacity.py:131`), and the table carries
`CheckConstraint("desired_unit > 0")`
(`packages/database/src/database/tables/compute.py:146`). Two writers of the same
object, one of which skips the field the other requires.

Twenty-five minutes of debugging produced no cause because nothing records one.
`disable-worker` carries only a worker id
(`packages/worker/src/worker/repository_payloads.py:123-124`; handler
`apps/api/src/api/server/worker_repository_service.py:823-827`; route
`apps/api/src/api/server/routers/worker_repository.py:290-297`), and
`SchedulerComputeHooks.disable_machine` accepts a `reason` and discards it
(`packages/scheduler/src/scheduler/compute_hooks.py:61-64`). The worker knows
exactly which step failed — `WorkerLifecycleStepResult.error_message`
(`packages/worker/src/worker/worker_lifecycle.py:594-598`) — and prints it to its
own stderr (`apps/container-worker/src/container_worker_app/main.py:285-291`)
before exiting.

Two authorities disagreed indefinitely and both were correct. The API said Ready
because `enrollment.readiness_phase is Ready and worker.status is
ResourceStatus.Running` (`packages/compute/src/compute/policy.py:580-585`) and
that durable `Running` is written the instant the worker POSTs add-worker
(`apps/api/src/api/server/worker_repository_service.py:751-761`). The scheduler
said unschedulable because the Redis record was `Pending`
(`packages/scheduler/src/scheduler/containers.py:1204-1210`, fit at
`packages/scheduler/src/scheduler/tools.py:185-229`). The same predicate is
copy-pasted into the reclaimer
(`packages/compute/src/compute/service.py:5137-5143`) rather than owned.

Being full marks a pool degraded. `AtLimit` maps to `CapacityReservationStatus.Failed`
(`packages/scheduler/src/scheduler/capacity_reservations.py:1728-1734`) and writes
`terminal_reason` (`:537-548`); any non-empty `terminal_reason` becomes `Degraded`
(`packages/scheduler/src/scheduler/pool_sizing.py:97-98`), which demotes the pool
in selection order (`pool_sizing.py:106-118`, used at `:1779-1786`).

Six places hold the same number: `CapacityProvisioningReservation.desired_unit`
(`capacity_reservations.py:162`), `pools.sizing_*`
(`packages/database/src/database/tables/orchestration.py:86-105`),
`compute_capacity_operations.desired_unit`
(`packages/database/src/database/tables/compute.py:162`),
`ComputePoolRecord.desired_machines` (`compute/service.py:1054-1080`), the
provider's `describe_pool().desired_machines` (`compute/service.py:461-476`), and
the count of open provider instance rows (`compute/service.py:400-412`). They are
reconciled by hand with `max()` at `capacity_reservations.py:434-438` and `:491`.

## Dependencies on other tracks

These are assertions I am making about the other three documents. If the named
capability does not appear there, the dependent CAP item below is blocked and
must be re-sequenced, not worked around.

**From `10-bootstrap-enrolment.md` (BOOT-*)**

- A BOOT item must own `MachineBootstrapPhase` becoming **node-reported only**,
  dropping the `Ready` member that no writer ever persists (verified: no writer
  at `compute/service.py:340,3095,3985,4429,4473,4504,4855` or
  `gateway/provider_enrollment.py` writes `Ready`; it exists solely as a
  read-time derivation at `compute/policy.py:586`). CAP-12 renames the derived
  verdict and cannot land before that enum decision is made in BOOT.
- A BOOT item should own promoting `bootstrap_phase` and `tailnet_phase` out of
  payload JSON into real columns
  (`packages/database/src/database/tables/compute.py:167-220` has only `status`).
  CAP-12 does not require it, but the reclaim behaviour CAP-12 touches is
  currently keyed on a field that cannot be queried
  (`compute/service.py:5113` → `packages/compute/src/compute/reclaim.py:102-113`).

**From `12-failure-semantics.md` (ERR-*)**

- An ERR item must define the **typed reason vocabulary and its durable
  surface** — the equivalent of `CapacityFailureCode`
  (`packages/shared/src/shared/capacity.py:16-50`) for worker and machine
  lifecycle. CAP-02 introduces `WorkerUnavailableReason` and must use that
  vocabulary's conventions (operator-safe text, bounded length, no provider
  exception strings), not invent a second one.
- An ERR item must decide how a scheduler `_best_effort_*` reconcile failure
  surfaces beyond a log line (`packages/scheduler/src/scheduler/service.py:484-602`,
  22 sequential `try/except/log/return []` passes per tick). CAP-06 and CAP-07
  make more conditions observable; without that decision they remain
  log-only.

**From `13-production-readiness.md` (INFRA-*)**

- An INFRA item must own **regenerating the single reviewed Alembic baseline**.
  CAP-10 and CAP-15 drop durable columns (`workers.version`, `pools.sizing_*`);
  under the predeployment rule they update the baseline and re-bootstrap rather
  than adding revisions. These two items must not be scheduled before that
  process is named.
- An INFRA item must own the metric/log surface the acceptance criteria below
  observe (worker unavailable-reason counts by reason, capacity acquisition
  outcome counts by status).

## Work items

- [ ] **CAP-01** Guard `_acquire_from_controller` against pending-worker reservations
  - **Files**: `packages/scheduler/src/scheduler/capacity_reservations.py:1266-1276`
  - **Change**: In `_acquire_from_controller`, the branch that fires when
    `not decision.created and reservation.status is not
    CapacityReservationStatus.Reserved` calls `controller.reconcile(...)`, which
    for `ComputePoolCapacityController` is `ensure_acquisition` (`:612-620`) and
    raises when `desired_unit <= 0` (`:592-600`). Reservations created by
    `reserve_pending` (`:1149-1189`) are persisted in `Provisioning` with the
    model default `desired_unit=0` (`:162`) and are reachable here because
    `reserve()` returns the container's existing allocation before any
    compatibility check (`:812-819`). Add the same source check `reconcile()`
    already applies at `:1538-1540`: when
    `reservation.source is CapacityReservationSource.PendingWorker`, do not call
    the controller — return a `CapacityAcquisitionResult` with
    `status=CapacityAcquisitionStatus.ExistingPending`,
    `desired_unit=reservation.desired_unit`,
    `target_worker_id=reservation.target_worker_id`, and
    `reason="pending worker capacity reservation is awaiting registration"`, and
    do not call `_record_acquisition_result`.
  - **Risk**: Low and bounded. The container is requeued as waiting instead of
    failing, so a genuinely dead pending worker now waits out
    `registration_deadline_at` (default 600s, `shared/capacity.py:198`) before
    `reconcile()` expires the reservation (`:1490-1537`) — slower to fail than
    an immediate error, but the immediate error was itself the outage. Blast
    radius is one method on the managed/pooled capacity path; agent pools and
    direct dispatch are untouched.
  - **Acceptance**: Reproduce the state directly against a local Redis: create a
    reservation via `reserve_pending` for a container, delete the pending worker
    record, then call `CapacityReservationService.acquire` for the same
    container. Before: `ValueError: compute capacity acquisition requires a
    persisted desired unit`. After: `ExistingPending`. Separately, on the live
    stack, drive one container through a worker that registers and then
    disables, and confirm the scheduler log contains no `capacity acquisition
    failed: ValueError` (`containers.py:657-675`) and the task error is not a
    Python type name.
  - **Depends on**: none

- [ ] **CAP-02** Carry a typed reason on every transition to `Unavailable`
  - **Files**: `packages/worker/src/worker/repository_payloads.py:123-125`;
    `apps/api/src/api/server/routers/worker_repository.py:290-297`;
    `apps/api/src/api/server/worker_repository_service.py:823-827`;
    `packages/scheduler/src/scheduler/state.py:726-753` (`update_worker_status`);
    `packages/shared/src/shared/scheduling.py:105-126` (`SchedulerWorkerRecord`);
    `packages/scheduler/src/scheduler/compute_hooks.py:61-64`
  - **Change**: Add `WorkerUnavailableReason` (a `StringEnum` in
    `shared/scheduling.py`, following the `CapacityFailureCode` conventions at
    `shared/capacity.py:16-50`) with at minimum: `RegistrationFailed`,
    `ReadinessValidationFailed`, `SourceCacheUnavailable`, `Draining`,
    `MachineRetired`, `AgentDisconnected`, `OperatorCordon`. Add
    `unavailable_reason: WorkerUnavailableReason | None` and
    `unavailable_detail: str` (bounded, operator-safe) to
    `SchedulerWorkerRecord`. Replace `WorkerIdRequest` on the disable route with
    a `DisableWorkerRequest` carrying `worker_id`, `reason`, and `detail`; thread
    it through the handler into `disable_worker`, and have
    `update_worker_status` persist both fields alongside the status. In
    `compute_hooks.disable_machine`, delete `_ = reason` and pass the reason
    through as `MachineRetired`. Do not keep the old bodiless route.
  - **Risk**: Contract change across the worker HTTP boundary — worker image and
    API must ship together. `SchedulerWorkerRecord` is serialised into a Redis
    hash (`state.py:632-641`), so records written by an older worker will lack
    the fields; under the predeployment rule, flush the scheduler's Redis worker
    keys rather than defaulting around it. Blast radius: every worker
    registration path plus the two internal disable callers
    (`agent_pool.py:141`, `workers.py:110,122`).
  - **Acceptance**: Cordon a worker through `lazycloud-admin`
    (`scheduler/workers.py:108-110`) and read the Redis worker hash: it carries
    `unavailable_reason=operator_cordon`. Retire an agent machine and confirm
    the same record carries `machine_retired`, not an empty field.
  - **Depends on**: ERR track's typed-reason vocabulary

- [ ] **CAP-03** Report which registration step failed
  - **Files**: `packages/worker/src/worker/worker_lifecycle.py:231-273,307-322,438-492`;
    `apps/container-worker/src/container_worker_app/main.py:278-300,366-372`;
    `packages/worker/src/worker/repository_client.py:982-993`
  - **Change**: `register_available` returns three ordered steps — `MarkAvailable`
    (`add_worker`), `ValidateReadiness` (the injected `readiness_validator`,
    wired to the gateway egress probe at
    `apps/container-worker/src/container_worker_app/production.py:1548-1555`),
    and `MarkAvailable` (`toggle_worker_available`) — and any one failing takes
    `main.py:279-300` to `ContainerWorkerRegistrationError`, whose only durable
    trace is stderr. Give `WorkerLifecycleOrchestrator.shutdown` a
    `reason: WorkerUnavailableReason` and `detail: str` parameter, thread it into
    `disable_scheduling` → `repository.disable_worker`, and have `main.py`'s
    `finally` block pass the reason derived from the first failed step:
    `ValidateReadiness` → `ReadinessValidationFailed`,
    `MarkAvailable`/`toggle_worker_available` → `SourceCacheUnavailable`,
    otherwise `RegistrationFailed`. `detail` is the step's `error_message`
    (`worker_lifecycle.py:594-598`), truncated to the bounded length CAP-02
    defines. Do not log-and-continue: the reason must reach the control plane on
    the same call that disables the worker.
  - **Risk**: The `detail` string originates from arbitrary exception text
    (`f"{type(exc).__name__}: {exc}"` at `worker_lifecycle.py:597`) and is now
    persisted and shown to operators. It must be bounded and must not carry URLs
    or tokens — the egress probe's failure text contains a gateway URL
    (`network_backend.py:399-403`). Send the exception **class name plus a fixed
    step description**, not the raw message, matching
    `capacity_failure_message` (`shared/capacity.py:39-50`).
  - **Acceptance**: On a live node, break the two candidates one at a time and
    confirm they are distinguishable **from the control plane alone**: (1) point
    the worker's gateway runtime URL at an unreachable origin and observe the
    worker record's `unavailable_reason=readiness_validation_failed`; (2) leave a
    source-cache cleanup target that cannot be purged
    (`source_cache_cleanup.py:190-213`) and observe
    `unavailable_reason=source_cache_unavailable`. The 25-minute ambiguity is
    resolved when these two produce different durable values without reading the
    worker's stderr.
  - **Depends on**: CAP-02

- [ ] **CAP-04** Remove the worker record when registration never completed
  - **Files**: `apps/container-worker/src/container_worker_app/main.py:279-300,366-372`;
    `packages/worker/src/worker/worker_lifecycle.py:438-492`
  - **Change**: `main.py:366-372` calls `shutdown(remove_worker=not
    resolved_settings.resolved_persistent)`, so a persistent worker whose
    registration failed leaves a `Pending` record holding the capacity it
    declared. That record is admitted to the scheduling candidate set
    (`containers.py:1204-1210`) and, if it declared capacity that fits, causes
    `plan_scheduling_batch` to return `WaitForWorker` (`tools.py:439-464`) and
    `containers.py:556-561` to call `reserve_pending` — the exact object CAP-01
    guards against. Pass `remove_worker=True` unconditionally on the
    registration-failure path (the branch reached via
    `ContainerWorkerRegistrationError`), keeping the persistence-dependent choice
    only for the normal spindown path. A worker that never became `Available`
    owns nothing worth preserving.
  - **Risk**: `remove_worker` requeues the worker's queued requests
    (`state.py:_cleanup_missing_worker_state`, requeue at `:689-700`); on a
    worker that never registered there are none, so this is inert. If a future
    change lets a worker hold requests before becoming available, this would
    requeue them — acceptable and correct. Blast radius: container-worker
    startup only.
  - **Acceptance**: Force a registration failure on a persistent worker and
    confirm via the scheduler worker list that no record for that worker id
    remains, and that the container that was waiting is not reported as
    `wait-for-worker` against it.
  - **Depends on**: CAP-03

- [ ] **CAP-05** Stop discarding the keep-alive source-cache outcome
  - **Files**: `packages/worker/src/worker/repository_client.py:890-912`;
    `apps/api/src/api/server/worker_repository_service.py:850-871`;
    `packages/worker/src/worker/worker_lifecycle.py:275-305`
  - **Change**: `set_worker_keep_alive` deliberately returns a typed outcome —
    `WorkerKeepAliveResponse(source_cache_state=generation.state)` with
    `worker=None` (`worker_repository_service.py:862-864`) — and the client
    throws `source_cache_state` away and raises
    `f"worker {worker_id!r} was not returned by repository"`
    (`repository_client.py:906-909`). `keepalive()` then treats that as a generic
    failure and calls `register_available()` (`worker_lifecycle.py:296-305`),
    issuing a fresh add-worker POST every interval (15s default,
    `main.py:49`). Make the client return the typed state: when `worker is None`
    and `source_cache_state` is set, raise a named
    `WorkerSourceCacheNotAvailableError` carrying the state, and have
    `keepalive()` map it to a `Skipped` step with that reason rather than
    re-registering. Re-registration stays only for the case where the worker
    record genuinely no longer exists.
  - **Risk**: A worker whose record was legitimately evicted (TTL 60s,
    `state.py:53`) must still re-register; make sure the distinguishing signal is
    the absent record, not the cache state. Getting this wrong leaves a worker
    permanently unregistered instead of permanently re-registering — a worse
    failure. Blast radius: worker keepalive loop.
  - **Acceptance**: Put a worker's source-cache generation into a non-available
    state and observe the add-worker POST rate at the API drop to zero while the
    worker stays alive, with the worker's step result naming the cache state.
    Then delete the worker's Redis record and observe exactly one re-registration.
  - **Depends on**: CAP-02

- [ ] **CAP-06** Give `mark_available` one job
  - **Files**: `packages/worker/src/worker/repository_client.py:869-889`;
    `packages/worker/src/worker/worker_lifecycle.py:215-229,231-273`;
    `packages/worker/src/worker/source_cache_cleanup.py:180-221`
  - **Change**: `toggle_worker_available` currently runs a full source-cache
    cleanup reconcile first and raises if any purge failed (`:876-883`), then
    requires `_available_worker`, which only `activate_source_cache()` sets
    (`:960-964`), which `WorkerSourceCacheReconciler.reconcile` calls only when
    `failed_count == 0` (`source_cache_cleanup.py:214-215`). So one un-purgeable
    source object silently means "this worker is never schedulable". Split it:
    add an explicit `ActivateSourceCache` step to
    `WorkerLifecycleAction` and to `register_available`'s sequence, running the
    reconcile there and reporting its own `WorkerSourceCacheReconcileResult`
    (which already carries `failure_detail`, `source_cache_cleanup.py:145-151`).
    Leave `toggle_worker_available` as a pure availability flip. The gate itself
    stays — see Open questions.
  - **Risk**: Reordering worker startup. The activation must still precede
    availability or a worker becomes schedulable with an un-purged cache, which
    is the data-loss condition the fencing exists to prevent
    (`source_cache_cleanup.py:70-138`). Blast radius: every worker start.
  - **Acceptance**: With one un-purgeable cleanup target, the worker's step
    results name `ActivateSourceCache` as the failing step and the durable
    reason is `source_cache_unavailable` (CAP-03), where previously the failure
    was attributed to `MarkAvailable`.
  - **Depends on**: CAP-03

- [ ] **CAP-07** Reclassify "at limit" as backpressure
  - **Files**: `packages/scheduler/src/scheduler/capacity_reservations.py:1728-1734,537-555`;
    `packages/scheduler/src/scheduler/pool_sizing.py:82-103`
  - **Change**: Three linked edits. (1) In `_record_acquisition_result`'s status
    map (`:1728-1734`), stop mapping `CapacityAcquisitionStatus.AtLimit` to
    `CapacityReservationStatus.Failed`; map it to the reservation's current
    status, as `TemporarilyUnavailable` already is. (2) In
    `_ensure_sizing_operation`'s `AtLimit` branch (`:537-548`), stop writing
    `terminal_reason`; clear the operation and record the target units only —
    the `WorkerPoolSizingPlan.reason` returned at `:549-555` already carries the
    explanation to the caller. (3) In `capacity_pool_operational_health`, the
    rule `if state.terminal_reason: return Degraded`
    (`pool_sizing.py:97-98`) must key on an explicit failure signal
    (`consecutive_failures > 0` or `retry_after_at` in the future, already
    checked at `:91-96`), not on any non-empty string. Being full must not demote
    the pool in `capacity_pool_selection_key` ordering
    (`pool_sizing.py:106-118`, used at `:1779-1786`).
  - **Risk**: A pool that is full and a pool that is broken currently look the
    same to selection; separating them means a full pool keeps its position and
    a request that could have failed over to a second pool may now retry against
    the full one first. Verify against the failover loop at `:1203-1224`, which
    only advances past a controller on a non-`Requested`/non-`ExistingPending`
    result — `AtLimit` still advances, so failover is preserved. Blast radius:
    multi-pool workspaces.
  - **Acceptance**: With a workspace holding two eligible pools, drive pool A to
    `max_workers` and submit a container. Observe: the request is placed on pool
    B; pool A's sizing state carries no `terminal_reason`; pool A's operational
    health is `Healthy`, not `Degraded`; and a subsequent request after pool A
    scales down selects pool A again by priority rather than by health penalty.
  - **Depends on**: none

- [ ] **CAP-08** Introduce one owned readiness predicate
  - **Files**: `packages/compute/src/compute/policy.py:550-609`;
    `packages/compute/src/compute/service.py:5105-5143`
  - **Change**: The predicate `enrollment.status is Active and
    enrollment.readiness_phase is Ready and worker is not None and worker.status
    is ResourceStatus.Running` is written out twice — once for the API summary
    (`policy.py:580-585`) and once for bootstrap reclaim
    (`service.py:5137-5143`). Extract a single function in `compute` —
    `machine_serves_workloads(session, *, workspace_id, machine_id, pool_name) ->
    bool` — and have both call it. Its body must read the **scheduler worker
    record** (`status is SchedulerWorkerStatus.Available`), not the durable
    `Worker` row; the compute service already holds a scheduler worker
    repository through its hooks
    (`packages/scheduler/src/scheduler/compute_hooks.py:31-42`), so route it
    through a narrow protocol rather than importing scheduler internals into
    compute.
  - **Risk**: The highest-risk item in the track. Reclaim decides whether to
    **terminate a billable machine** (`service.py:5105-5143`), and it currently
    passes as soon as the durable `Worker` says `Running` — which CAP-09 makes
    strictly harder to satisfy. If the scheduler's Redis worker state is
    unavailable at reclaim time, the predicate must not read false and terminate
    a healthy fleet. Make repository unavailability raise rather than return
    `False`, and confirm the caller treats a raised reclaim check as "do not
    reclaim". Blast radius: every managed machine.
  - **Acceptance**: With a machine whose agent heartbeats but whose worker is
    `Pending`, confirm both consumers now agree: the API summary reports it as
    not-ready, and the reclaim path selects `WorkerReadinessFailed`. Then stop
    Redis and confirm the reclaim pass terminates nothing and logs the
    unavailability.
  - **Depends on**: none

- [ ] **CAP-09** Stop asserting durable `Worker.status = Running` at registration
  - **Files**: `apps/api/src/api/server/worker_repository_service.py:732-767`
  - **Change**: `_sync_runtime_worker_registration` upserts the durable worker
    with `status: ResourceStatus.Running` (`:756`) on every add-worker — before
    readiness validation, before source-cache activation, before the worker is
    schedulable. Remove `status` from that update; keep `machine_id`, `pool`, and
    `last_seen_at`. The durable row becomes inventory. Any consumer that needs
    readiness calls CAP-08's function.
  - **Risk**: Any reader of `Worker.status` that assumed registration implied
    `Running` now sees the enrollment-time value (`ResourceStatus.Created`,
    `gateway/service.py:1660-1663`). Both known readers are the two call sites
    CAP-08 unified. Audit for others before landing — a missed reader silently
    changes a machine's reported state. Blast radius: compute summary and
    bootstrap reclaim.
  - **Acceptance**: Register a worker and observe the durable row's status is
    unchanged by registration, while the API summary's ready count is driven by
    the scheduler record. A worker that registers and immediately disables must
    never appear in `ready_instance_count` (`policy.py:385-387`).
  - **Depends on**: CAP-08

- [ ] **CAP-10** Delete `Worker.version`
  - **Files**: `packages/shared/src/shared/compute_fleet.py:48-55`;
    `packages/gateway/src/gateway/service.py:1658-1668`;
    `packages/compute/src/compute/service.py:3464-3492` (`register_worker`);
    the `workers` table column and its baseline
  - **Change**: `version` is written once as the literal `"pending"` at
    `gateway/service.py:1664-1666` and preserved verbatim by every later upsert;
    no code anywhere reads it for any decision (grep-verified). It is a
    write-only field that reads like a state and cost a live debugging session.
    Remove the field from the `Worker` contract, the `register_worker` parameter,
    both write sites, and the durable column; update the reviewed Alembic
    baseline and re-bootstrap.
  - **Risk**: If any dashboard or CLI column renders it, that surface must be
    updated in the same change. Blast radius: schema.
  - **Acceptance**: `grep -rn "\.version" packages/gateway packages/compute
    apps/api --include=*.py` returns no worker-related hit, and a fresh bootstrap
    plus one machine enrolment succeeds with no `version` column present.
  - **Depends on**: CAP-09, INFRA baseline-regeneration item

- [ ] **CAP-11** Delete the dead projection status cluster
  - **Files**: `packages/compute/src/compute/projection.py:36-48,205-230,418-504`
  - **Change**: Delete `WorkerStatus` (`:36-41`), `MachineStatus` (`:43-48`),
    `WorkerCapacityState` (`:205-216`), `MachineProjection` (`:218-230`),
    `project_agent_machine` (`:418-442`), `project_agent_machine_metrics`
    (`:445-478`), `agent_machine_public_status` (`:480-497`), and
    `capacity_utilization_pct` (`:499-504`). All eight have **zero references
    outside this file** (verified). Note `compute/agent_control.py:96` defines a
    *different, live* `WorkerStatus` — the gateway imports that one
    (`packages/gateway/src/gateway/views.py:5-8`); do not touch it. Keep the rest
    of `projection.py`, which has many live consumers. Remove now-unused imports
    (`AgentMachineMetrics` and `format_compute_time` become unreferenced within
    the module — confirm before deleting either).
  - **Risk**: Minimal. The only real hazard is deleting the wrong `WorkerStatus`;
    the two differ by module and by member set (`projection` has `Unknown`,
    `agent_control` does not).
  - **Acceptance**: `uv run ruff check packages/compute` and an import of every
    module that imports `compute.projection` (19 call sites) both succeed; the
    compute pool and agent-machine API responses are byte-identical before and
    after.
  - **Depends on**: none

- [ ] **CAP-12** Split the persisted bootstrap phase from the derived verdict
  - **Files**: `packages/shared/src/shared/compute_enrollment.py:44-51`;
    `packages/compute/src/compute/policy.py:550-609,378-408`;
    `packages/compute/src/compute/reclaim.py:43-54,102-113`;
    `apps/api/src/api/server/routers/resource_api/compute_policy.py:145-155`
  - **Change**: `MachineBootstrapPhase` is simultaneously a persisted
    node-reported progress value and a user-facing verdict, and its `Ready`
    member exists only in the derivation at `policy.py:586` — no writer persists
    it. Remove `Ready` from `MachineBootstrapPhase`. Add
    `MachineServiceState` (`Provisioning`, `Joining`, `Serving`, `Degraded`,
    `Failed`, `Deleting`) as the derived type, computed by `_compute_instance_view`
    from the stored phase plus CAP-08's `machine_serves_workloads`. `ComputeSummary`
    (`policy.py:88-97,378-408`) and the API response
    (`compute_policy.py:145-155`) report `MachineServiceState`;
    `ComputeReclaimPolicy.phase_deadline_for` keeps reading the stored phase
    unchanged.
  - **Risk**: Public API contract change — `bootstrap_phase` in the compute
    response changes meaning and loses a member. The web Zod schemas
    (`apps/web/src/lib/api/schemas/`) must change in the same commit, per the
    repository's contract-synchronisation rule. Blast radius: compute UI and any
    SDK consumer of instance status.
  - **Acceptance**: The compute instances endpoint reports `Serving` only for
    machines whose scheduler worker is `Available`. A machine whose agent
    heartbeats but whose worker crash-loops reports `Joining`, and the reported
    value matches what the reclaimer is measuring against.
  - **Depends on**: CAP-08, BOOT item owning `MachineBootstrapPhase`

- [ ] **CAP-13** Delete the pending-worker reservation path
  - **Files**: `packages/scheduler/src/scheduler/capacity_reservations.py:1149-1189,79-81,1538-1540,226-246,1802-1828`;
    `packages/scheduler/src/scheduler/containers.py:549-569,237-251`
  - **Change**: `reserve_pending` exists to stop a second request provisioning
    while a pending worker boots. `plan_scheduling_batch` already does that
    in-memory by reserving against pending workers (`tools.py:439-464`); the
    durable half contributes only the `desired_unit=0` object that caused the
    outage. Delete `CapacityReservationService.reserve_pending` (`:1149-1189`),
    the `CapacityReservationSource` enum and its field (`:79-81`, `:161`), the
    `PendingWorker` branch in `reconcile` (`:1538-1540`), the guard CAP-01 added,
    `PendingCapacityOwner` (`:226-246`) and `_pending_owner_for_worker`
    (`:1802-1828`) if no other caller remains, the `pending_owners` constructor
    field (`:1120`), and the `reserve_pending` call and protocol member in
    `containers.py` (`:556-569`, `:237-251`). The `WaitForWorker` outcome then
    requeues without creating durable state, which is what the in-memory
    reservation already implies.
  - **Risk**: Real. The durable claim survives a scheduler restart; the in-memory
    one does not. Two scheduler replicas, or one replica restarting mid-boot of a
    pending worker, could each decide to provision. Quantify before landing: if
    the scheduler is single-replica and the requeue delay is short relative to
    worker boot, the exposure is one extra machine per restart during a boot
    window. If that is unacceptable, this item must instead be rewritten to make
    the pending-worker claim carry a real `desired_unit`, and CAP-01's guard
    stays permanently. **This is the item most likely to be wrong.**
  - **Acceptance**: Submit ten concurrent containers against an empty pool with
    `max_workers >= 10` and confirm exactly one machine is launched
    (`compute_capacity_operations` holds one row). Repeat while restarting the
    scheduler once mid-boot and record how many machines are launched — that
    number is the decision input for the risk above.
  - **Depends on**: CAP-01

- [ ] **CAP-14** Collapse acquisition to one idempotent entry point
  - **Files**: `packages/scheduler/src/scheduler/capacity_reservations.py:283-320,574-664,1266-1291`;
    `packages/compute/src/compute/service.py:348-484`
  - **Change**: `CapacityAcquisitionController` exposes `plan_acquisition`,
    `ensure_acquisition` and `reconcile` (`:283-304`), whose implementations are
    `plan_capacity_acquisition` then `acquire_capacity` with a persist in between
    (`:574-620`) — the two-phase split exists solely to make the desired unit
    durable before the provider call. `compute_capacity_operations` already does
    that inside compute's own transaction, with a uniqueness constraint on
    `reservation_id` and `desired_unit > 0`
    (`packages/database/src/database/tables/compute.py:144-146`). Replace all
    three with one `ensure_capacity(reservation) -> CapacityAcquisitionResult`
    that calls a single compute method combining `plan_capacity_acquisition` and
    `acquire_capacity` (`compute/service.py:348-484`) behind one session. Delete
    `CapacityAcquisitionPlanningRequest` (`shared/capacity.py:114-118`) and the
    `_record_acquisition_result` call that exists only to persist the plan
    (`:1282-1286`).
  - **Risk**: This changes where the fencing lives. The current design persists
    the scheduler's intent before the provider call so a crash between plan and
    act cannot double-buy; moving that entirely into compute means the
    scheduler's Redis reservation is no longer a fence. Confirm the
    `uq_compute_capacity_operations_reservation` constraint plus the
    `operation is not None` short-circuit (`compute/service.py:434-446`) is
    sufficient for the retry case before deleting the scheduler-side persist.
    Blast radius: all managed capacity acquisition.
  - **Acceptance**: Kill the scheduler between the compute call and the Redis
    write, restart, and confirm the retry produces the same operation row and
    launches no second machine. Count rows in `compute_capacity_operations` for
    the reservation: exactly one.
  - **Depends on**: CAP-13

- [x] **CAP-15** Delete `CapacityPoolSizingState` and the `pools.sizing_*` columns
  - **Files**: `packages/shared/src/shared/capacity.py:231-269`;
    `packages/database/src/database/tables/orchestration.py:57,86-105`;
    `packages/database/src/database/repositories/orchestration.py:87-122,186-200`;
    `packages/compute/src/compute/service.py:1493-1510`;
    `packages/scheduler/src/scheduler/capacity_reservations.py:418-572,1868-1911`;
    `packages/scheduler/src/scheduler/pool_sizing.py:73-79,271-317`
  - **Change**: This is the third of six copies of the desired unit and exists
    only to keep the other two consistent — `_sizing_state` reconciles it with
    `max()` (`:434-438`) and `_ensure_sizing_operation` re-derives it from a
    compute plan (`:491`). After CAP-14, the authoritative pair is the provider's
    desired count and the `compute_capacity_operations` rows. Delete
    `CapacityPoolSizingState`, `CapacityPoolSizingStateUpdate`, the ten
    `sizing_*` columns and their check constraint, the repository accessors, the
    two compute service methods, `_sizing_state` / `_record_sizing_observation` /
    `_save_sizing_state` (`:1868-1911`), and `ComputePoolCapacityController.reconcile_sizing`
    / `_ensure_sizing_operation` (`:418-572`). Keep the **pure** functions
    `plan_worker_pool_sizing` and `effective_pool_headroom`, re-pointing them at
    an in-memory state derived from open operation rows.
  - **Risk**: Highest-cost item, and it deletes the only cross-restart backoff.
    `consecutive_failures` and `retry_after_at` (`shared/capacity.py:242-243`)
    are what stop a pool with a broken launch path from relaunching billable
    machines every tick; `sizing_failure_state` (`pool_sizing.py:271-285`)
    computes the exponential backoff. That behaviour must be reconstructed from
    `compute_capacity_operations` (which records failures) **before** the columns
    are dropped, or a systematic bootstrap failure becomes a billing incident.
    `ComputeReclaimPolicy.max_launch_attempts` (`compute/reclaim.py:56-62`) is a
    second, independent bound and partly covers this — confirm it does before
    relying on it. Blast radius: cost.
  - **Acceptance**: With a pool whose provider launch always fails, observe the
    launch attempt interval over 30 minutes and confirm it backs off
    exponentially and stops at the launch-attempt bound, with no more machines
    created than before this change. Compare against a recorded baseline taken
    before the change.
  - **Depends on**: CAP-14, INFRA baseline-regeneration item
  - **Outcome**: Done ahead of CAP-14, which was not needed: the authoritative
    pair already exists. `CapacityPoolSizingSnapshot` (`shared/capacity.py`) is
    derived on every read by `ComputeService.pool_sizing_snapshot` and stored
    nowhere. Backoff is reconstructed from a new `failure_count` on the capacity
    operation row plus its `updated_at`, and `scale_up_retry_at`
    (`pool_sizing.py`) recomputes the same interval a restarted scheduler would
    have held. `initial_target_reached` becomes `peak_desired_unit` over the
    owner's operation rows, released ones included, so a drained pool is not
    bought back to `initial_workers`. Scale-down history — the gap that made the
    remaining scope look unexecutable — comes from the provider machine records,
    not the operation rows, because `release_internal_pool_machine` and
    `terminate_pool_machine` write no operation row. `pool_drain.py` was a
    writer of the deleted state and is missing from the Deletions table above.
    One behaviour is genuinely lost: a provider that fails during planning,
    before any operation row exists, no longer backs off between sizer ticks. It
    creates no machine on that path, and the tick is still spaced by
    `scale_up_cooldown_seconds`. The 30-minute always-failing-pool acceptance
    still needs the connected AWS environment and has not been run; the cost
    bound it measures is `max_launch_attempts` → degraded (04bd830), which is
    durable on the pool row and independent of everything deleted here.

- [ ] **CAP-16** Collapse the reservation status machine
  - **Files**: `packages/scheduler/src/scheduler/capacity_reservations.py:70-77,149-186,942-1058,2045-2070`
  - **Change**: Replace the six-member `CapacityReservationStatus` (`:70-77`) with
    `Waiting` and `Closed`. `Reserved`/`Provisioning`/`Registered` become
    `Waiting` plus the already-present `target_worker_id`;
    `Failed`/`Expired`/`Released` become `Closed` plus a typed reason using
    CAP-02's vocabulary. Delete `_allowed_next_statuses` (`:2045-2070`),
    `release_requested`, `release_target_unit` and `acquisition_created`
    (`:163-165`) together with `prepare_release` (`:969-990`) — after CAP-14
    the unit's ownership is the `compute_capacity_operations` row, not a boolean
    on the reservation. Keep `resource_version` CAS in `update` (`:942-967`).
  - **Risk**: `release_terminal` (`:1003-1035`) currently performs a two-step
    transition (to `Failed`, then `Released`) to satisfy the transition graph;
    collapsing it must not lose the distinction between "released after
    registering" and "released without ever registering", which
    `_release_unallocated_reservation` (`:1616-1682`) branches on. Blast radius:
    reservation lifecycle and capacity release — a mistake here leaks machines.
  - **Acceptance**: Run a full container lifecycle (submit → provision → run →
    complete) and a full abandonment (submit → provision → registration deadline
    expires) and confirm in both cases that the provider instance count returns
    to its starting value and no `compute_capacity_operations` row is left
    non-terminal.
  - **Depends on**: CAP-15

- [ ] **CAP-17** Split `capacity_reservations.py` along its five-way seam
  - **Files**: `packages/scheduler/src/scheduler/capacity_reservations.py` (whole file)
  - **Change**: The file already has clean internal boundaries. Split into
    `capacity_contracts.py` (the enums, `CapacityRequestShape`,
    `CapacityProvisioningReservation`, `CapacityAcquisitionResult`, the error
    types — currently `:70-247`), `capacity_controller.py` (the
    `CapacityAcquisitionController` protocol and `ComputePoolCapacityController`
    — currently `:249-664`, reduced to roughly a third by CAP-14 and CAP-15),
    `capacity_store.py` (`CapacityReservationKeys` and
    `RedisCapacityReservationRepository` — currently `:667-1114`),
    `capacity_service.py` (`CapacityReservationService` — currently `:1116-1828`),
    and `capacity_matching.py` (the shape/worker matching free functions —
    currently `:1831-2101`). Move only; no behaviour change. Update
    `packages/scheduler/src/scheduler/__init__.py` exports and the importers in
    `containers.py`, `service.py`, `services.py` and `apps/scheduler`.
  - **Risk**: Import cycles. `capacity_service` needs `capacity_store` and
    `capacity_controller`; `capacity_controller` needs `pool_sizing` and
    `capacity_contracts`; keep the dependency direction strictly one-way. A
    pure move landed in the same commit as behaviour changes would be
    unreviewable — land it alone.
  - **Acceptance**: `git diff --stat` shows only moves and import edits; the
    scheduler owner test suite result is identical before and after; a live
    container dispatch on the local stack succeeds unchanged.
  - **Depends on**: CAP-16

## Deletions

| File or symbol | Lines | Why safe to delete | What replaces it |
| --- | --- | --- | --- |
| `compute/projection.py` `WorkerStatus` | 36-41 | Zero references outside the file; the live `WorkerStatus` is `compute/agent_control.py:96` | Nothing; `agent_control.WorkerStatus` already serves the gateway |
| `compute/projection.py` `MachineStatus` | 43-48 | Zero references outside the file | `MachineServiceState` (CAP-12) |
| `compute/projection.py` `WorkerCapacityState` | 205-216 | Zero references outside the file | `SchedulerWorkerRecord` |
| `compute/projection.py` `MachineProjection` | 218-230 | Zero references outside the file | `ComputeInstanceView` (`policy.py:72-79`) |
| `compute/projection.py` `project_agent_machine`, `project_agent_machine_metrics`, `agent_machine_public_status`, `capacity_utilization_pct` | 418-504 | Zero references outside the file | `_compute_instance_view` (`policy.py:550-609`) |
| `shared/compute_fleet.py` `Worker.version` + `workers.version` column | 52 | Written once as `"pending"` (`gateway/service.py:1664-1666`), never read for any decision | Nothing; it encoded no state |
| `MachineBootstrapPhase.Ready` | `shared/compute_enrollment.py:49` | No writer persists it; exists only in the derivation at `policy.py:586` | `MachineServiceState.Serving` (CAP-12) |
| `CapacityReservationService.reserve_pending` + `CapacityReservationSource` + `PendingCapacityOwner` + `_pending_owner_for_worker` | `capacity_reservations.py:1149-1189, 79-81, 226-246, 1802-1828` | Its exclusion role is already served in-memory by `plan_scheduling_batch` (`tools.py:439-464`); its durable half only produced the `desired_unit=0` object | In-memory pending reservation, subject to CAP-13's restart measurement |
| `plan_acquisition` / `ensure_acquisition` / `reconcile` triple | `capacity_reservations.py:283-304, 574-620` | The two-phase persist is duplicated by `compute_capacity_operations` and its uniqueness constraint (`tables/compute.py:144-146`) | Single `ensure_capacity` (CAP-14) |
| `CapacityAcquisitionPlanningRequest` | `shared/capacity.py:114-118` | Only input to the deleted planning phase | `CapacityAcquisitionRequest` |
| `CapacityPoolSizingState` / `CapacityPoolSizingStateUpdate` + `pools.sizing_*` (10 columns) + repository accessors + compute accessors | `shared/capacity.py:231-269`; `tables/orchestration.py:57,86-105`; `repositories/orchestration.py:87-122,186-200`; `compute/service.py:1493-1510` | Third of six copies of the desired unit, reconciled by hand with `max()` (`capacity_reservations.py:434-438,491`) | Provider desired count + `compute_capacity_operations` rows; backoff must be reconstructed first (CAP-15 risk) |
| `ComputePoolCapacityController.reconcile_sizing` / `_ensure_sizing_operation` / `_sizing_state` / `_record_sizing_observation` / `_save_sizing_state` | `capacity_reservations.py:418-572, 1868-1911` | Exist only to maintain the deleted state | `plan_worker_pool_sizing` over in-memory state (kept, pure) |
| `_allowed_next_statuses`, `prepare_release`, `release_requested`, `release_target_unit`, `acquisition_created` | `capacity_reservations.py:2045-2070, 969-990, 163-165` | Guard a six-state machine reduced to two states; unit ownership moves to the operation row | `Waiting`/`Closed` plus typed reason (CAP-16) |
| `WorkerIdRequest` on the disable route | `routers/worker_repository.py:290-297` | Cannot carry a reason; every disable is currently anonymous | `DisableWorkerRequest` with typed reason (CAP-02) |

## Track acceptance

The track is production ready when all of the following are observed on a live
stack with at least one managed EC2 pool, not in a unit suite.

1. **The reported outage cannot recur.** Drive a container through a worker that
   registers and then disables before becoming available. The task fails or
   waits with a reason naming the capacity condition; the scheduler log contains
   no Python exception type name in any user-visible reason
   (`containers.py:657-675`).

2. **A worker crash-loop names its own cause from the control plane.** Break the
   gateway egress probe on one node and an un-purgeable source-cache target on
   another. Query the two worker records and read two different
   `unavailable_reason` values. No stderr, no SSH, no log grep. This is the
   specific ambiguity that cost the debugging session.

3. **Ready means schedulable.** A machine whose agent heartbeats but whose worker
   never becomes `Available` is never reported as ready by the compute summary,
   the compute instances endpoint, or the dashboard. Confirm against
   `ready_instance_count` (`policy.py:385-387`) and the API response.

4. **Full is not broken.** With two eligible pools and pool A at `max_workers`,
   requests place on pool B, pool A's health stays `Healthy`, and pool A is
   selected again by priority once it has room.

5. **One machine per demand.** Ten concurrent containers against an empty pool
   launch exactly one machine per unit of unmet demand and no more; verified by
   row count in `compute_capacity_operations` and by the provider's instance
   list. Repeat with a scheduler restart mid-boot and record the result — that
   number is the CAP-13 decision input and must be reported to the owner, not
   assumed.

6. **No capacity leaks.** Run both a completed container lifecycle and an
   abandoned one (registration deadline expiry). In both, the provider instance
   count returns to baseline and no `compute_capacity_operations` row is left
   non-terminal.

7. **Failure is still bounded.** With a pool whose provider launch always fails,
   the launch interval backs off exponentially and stops at the launch-attempt
   bound. Compare machine-creation count over 30 minutes against a baseline
   captured before CAP-15. This is the cost regression guard.

8. **Nothing dead remains.** `grep` confirms no reference to any symbol in the
   Deletions table, and a fresh PostgreSQL bootstrap from the updated baseline
   succeeds with a machine enrolling and a container running end to end.

## Open questions for the owner

1. **CAP-13 is the item I would not do.** Deleting `reserve_pending` removes a
   durable claim and replaces it with an in-memory one. I could not establish
   from the code whether the scheduler runs single-replica in production; if it
   does not, or if restarts are frequent relative to worker boot time, this
   trades a fixed one-line guard (CAP-01) for an intermittent duplicate-machine
   cost. The measurement in CAP-13's acceptance should gate the decision, and
   the honest alternative is to keep the pending-worker reservation and give it
   a real `desired_unit` instead. **Decide after the measurement, not before.**

2. **Should source-cache cleanup gate worker availability at all?** Today one
   un-purgeable object makes a worker permanently unschedulable
   (`source_cache_cleanup.py:214-215` → `repository_client.py:876-889`). The
   fencing itself is well built and prevents real data loss, and CAP-06
   deliberately keeps the gate while making the failure legible. But holding a
   whole machine out of service for a cleanup backlog is a strong policy choice
   that is currently implicit. Alternatives: bound it by attempt count and let
   the worker serve while cleanup is retried; or keep it hard and add an alert.
   This is a data-safety-versus-availability call and belongs to you.

3. **Is the full programme justified?** My audit concluded it was not — every
   defect found (CAP-01, CAP-03, CAP-07, CAP-09, CAP-11) is independently
   fixable in under a day inside the existing shape, and CAP-01 alone ends the
   live outage. CAP-13 through CAP-17 are the structural half; they carry the
   real risk (cost regressions in CAP-15, capacity leaks in CAP-16) and none of
   them fixes a defect that has actually fired. You have chosen the full
   programme and that is your call; I am recording that CAP-01 through CAP-12
   would deliver most of the reliability gain, and that a sensible stop point
   exists after CAP-12.

4. **How many scheduler replicas run in production?** This determines CAP-13,
   whether the per-owner Redis mutation lock and renewal thread
   (`capacity_reservations.py:732-795`) is load-bearing or ceremony, and whether
   CAP-14's move of fencing into compute is safe. I could not answer it from the
   code.

5. **Does anything outside this repository read `bootstrap_phase`?** CAP-12
   removes a member from a public enum. If an external consumer or a saved
   dashboard query depends on `"ready"` appearing there, the rename needs a
   coordinated change I cannot see from here.
