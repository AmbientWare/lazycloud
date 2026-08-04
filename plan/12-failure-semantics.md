# Failure Semantics, Persistence and Configuration

## Target state

A failure has exactly one representation at each hop, and no hop invents one.

Inside a package, a failure is an exception. It is raised with `raise X(...) from
exc` — already true at 503 of 504 sites — and it is never converted to a value
without first reaching a sink. A blind `except` may do one of two things: re-raise,
or call `LOGGER.exception(...)` and then convert. There is no third option, and
`ruff BLE001` says so mechanically.

At a service boundary a failure becomes a `DomainError` carrying a stable `code`
alongside its message, so the machine-readable classification that exists today
inside `ManagedComputeLaunchError` survives instead of being concatenated into a
string. The API's `DomainError` sink logs `exc` with its `__cause__` chain and
returns `{detail, code, request_id}`; the caller gets a code it can branch on and
an id that finds the traceback. Nothing reaches a client as a bare 500 because a
service raised a `RuntimeError` subclass nobody mapped.

A durable failure state carries its own reason. A status enum names a category
(`bootstrap_timed_out`); a sibling free-text field names the cause. The enum is
never asked to do both, and never grows a member to describe one incident.

A persisted record cannot hold a value its own annotation forbids.
`_TableRecordStore._upsert` revalidates before it writes, so the 345
`model_copy(update=...)` sites — the codebase's universal mutation idiom — cannot
put a raw `str` into an enum field and have it survive to disk. `pytest -W
error::UserWarning` makes any regression a test failure rather than a log line.

Configuration is edited server-side against the stored record. A client never
reconstructs a whole policy from ten stored values plus one flag, so a
cross-field invariant fails as a 422 naming the conflict, not as a raw
`ValidationError` in the CLI process.

## Why this is simpler and more reliable

**The exception-native path is already excellent; only the exception-to-value
path leaks.** Measured by AST over 724 production files / 213,641 LOC: 503 of 504
raises inside `except` blocks chain with `from` (99.8%); `ruff TRY400` reports
zero violations; all 84 `shared.errors` raises inside handlers are chained. The
fix is therefore not a rewrite of error handling — it is closing the sinks that
throw away what the raise sites correctly preserved.

**The loss is concentrated and countable.** Of 338 broad handlers, 123 propagate
and 215 convert to a value; of those 215, only 37 log a traceback — **178 (83%)
discard it**. `ruff BLE001` independently finds 194, corroborating the count. The
durable event channel that should catch these (`packages/observability/src/observability/events.py:36-45`)
is used by **2 of 338**.

**One sink fix recovers every chained cause.** `apps/api/src/api/fastapi_app.py:219-224`
emits `exc.message` and logs nothing, while `apps/api/src/api/fastapi_app.py:242-256`
already does the right thing for unexpected errors (`logger.exception(..., exc_info=exc)`
plus a correlating request id). Copying the second into the first is a
five-line change that makes all 84 chained domain raises debuggable.

**The persistence fix is one line and is verified.** `_TableRecordStore._create`
validates (`packages/database/src/database/repositories/common.py:90`);
`_TableRecordStore._upsert` does not (`common.py:96-128`). That asymmetry is the
whole bug class. I prototyped `model_validate(dict(model))` in `_upsert` and ran
`packages/` : **1126 passed, 129 skipped, 0 failures**; and with the warning gate
on, `packages/compute packages/database -W error::UserWarning` went from **5
failed** to **121 passed**. The `dict(model)` form matters — `model_dump(mode="python")`
invokes the serializer and emits the very warning being gated on.

**The drift is real, reachable, and already exercised by existing tests.**
`packages/compute/src/compute/service.py:3985-3988` writes `.value` strings into
`bootstrap_phase`/`bootstrap_failure_reason`, which are enum-typed at
`packages/database/src/database/repositories/compute.py:129-130`, then applies
them via `model_copy` at `service.py:4004`. The same fields receive enum objects
at `service.py:4474` and `:4505`. Durable JSON is not corrupted (`StringEnum` is
`str, Enum`), but `is` identity breaks — and production uses `is` on enums **992
times**. `ComputePoolRepository.upsert` already carries the correct guard at
`packages/database/src/database/repositories/compute.py:381`, which is why the
structurally identical site at `service.py:4263` is harmless and `:4004` is not.
The lesson exists in the repo; it was never generalized.

**Three sites read as protection and provide none.**
`packages/database/src/database/repositories/compute.py:523`, `:545`, and
`packages/compute/src/compute/service.py:2085` call
`ComputePoolRecord.model_validate(<a ComputePoolRecord>)`. Pydantic v2 returns the
same object unvalidated when the input is already an instance
(`revalidate_instances` defaults to `"never"`) — verified empirically.

**The config surface is justified and should not be cut.** All 11 compute-policy
knobs have real production read sites; every field carries a `ge`/`le`/`pattern`
constraint (`packages/shared/src/shared/compute_policy.py:83-95`). The defects are
two silent interactions, not knob count.

## Dependencies on other tracks

I am asserting these; the sibling documents did not exist when this was written.

- **BOOT-\*** — I add `bootstrap_failure_detail` beside the existing enum (ERR-11).
  I own the field on the record and its exposure through the API. **BOOT must own
  the producer**: whatever classifies a bootstrap failure has to populate the
  detail string. Until BOOT lands, the field exists and is empty, which is
  strictly better than today but is not the outcome. ERR-11 must land *before*
  the BOOT work that fills it.
- **CAP-\*** — three couplings:
  1. **`disable-worker` reason (ERR-12).** I own the contract change only
     (`WorkerIdRequest` → a request carrying a reason, plus route and service
     signature). The caller-side wiring at
     `packages/worker/src/worker/worker_lifecycle.py:318` and
     `packages/gateway/src/gateway/service.py:1016` is CAP's. Do not duplicate.
     ERR-12 must land before CAP wires callers.
  2. **At-limit convention (ERR-10).** Unifying the three conventions changes
     `plan_capacity_acquisition` and the pooled-scale path, which CAP also edits.
     ERR-10 should land *before* CAP's capacity work, or be merged into it.
  3. **`ComputeService` decomposition (ERR-16).** I propose extracting only the
     provider-reconciliation seam (~1,100 lines). **The capacity-acquisition seam
     (~1,500 lines: `_acquire_direct_capacity`, `_acquire_pooled_capacity`,
     `_release_pooled_capacity`, `_prepare_pooled_capacity`) is CAP's, not mine.**
     If CAP is also splitting this class, ERR-16 must be sequenced against it —
     these cannot land concurrently in a 4,968-line class.
- **INFRA-\*** — the two gates in ERR-02 and ERR-38 are only real if CI runs
  them. INFRA must run `ruff check` and `pytest` with the repository config in
  the pipeline. If CI does not currently fail on ruff, ERR-38 is decorative.

## Work items

- [ ] **ERR-01** Revalidate every record on the upsert path
  - **Files**: `packages/database/src/database/repositories/common.py:96-128` (`_TableRecordStore._upsert`)
  - **Change**: as the first statement of `_upsert`, before `payload = _json_object(model)`, insert
    `model = self.config.model_type.model_validate(dict(model))`.
    Use `dict(model)` specifically — it reads raw field values via Pydantic's
    `__iter__` without invoking the serializer. Do **not** use
    `model.model_dump(mode="python")`: that serializes, and on a drifted record it
    emits the exact `UserWarning` that ERR-02 turns into an error, so the guard
    would trip the gate it is meant to make adoptable.
  - **Risk**: every write path in the repository layer now runs one extra
    validation per upsert. Blast radius is total — 36 `upsert` methods, every
    domain record. A record whose in-memory state violates its own annotation will
    now raise `ValidationError` on write instead of silently persisting; that is
    the intent, but it converts latent corruption into a loud failure and could
    surface unknown violations in packages I did not exercise.
  - **Acceptance**: `uv run pytest packages -q -p no:randomly` → 1126 passed, 129
    skipped, 0 failed (this is the observed baseline with the change applied, not a
    projection). Then `uv run pytest packages/compute packages/database -q -W error::UserWarning`
    → 121 passed, 0 failed, where the same command without the change fails 5.
  - **Depends on**: none

- [ ] **ERR-02** Make Pydantic serializer warnings a test failure
  - **Files**: `pyproject.toml:` `[tool.pytest.ini_options]` block (currently
    `pythonpath`/`testpaths`/`norecursedirs`/`addopts`, no `filterwarnings`)
  - **Change**: add `filterwarnings = ["error::UserWarning"]`. Pydantic emits
    `PydanticSerializationUnexpectedValue` as a plain `UserWarning` (verified), so
    this is the category that catches type drift. Do not add per-file ignores or
    `-W` overrides in individual suites; CLAUDE.md forbids exclusions.
  - **Risk**: any third-party library emitting a benign `UserWarning` now fails the
    suite. I verified `packages/` is clean under this filter *with ERR-01 applied*;
    I did **not** run `apps/` or root `tests/` under it. Expect to fix or narrow
    the category once if a dependency is noisy — narrow by warning class, never by
    file.
  - **Acceptance**: `uv run pytest packages apps -q -p no:randomly` passes with the
    filter in place. Then confirm the gate bites: temporarily restore
    `"bootstrap_phase": bootstrap_phase.value` at
    `packages/compute/src/compute/service.py:3985` and observe
    `packages/compute/tests/test_pooled_capacity.py` fail with
    `PydanticSerializationUnexpectedValue ... field_name='bootstrap_phase'`; revert.
  - **Depends on**: ERR-01

- [ ] **ERR-03** Stop building one payload dict for two different contracts
  - **Files**: `packages/compute/src/compute/service.py:3966-4004` (`_sync_pooled_instances`), `packages/compute/src/compute/service.py:4240-4263` (`_ensure_compute_pool_record`)
  - **Change**: at `service.py:3985-3988` replace `"bootstrap_phase": bootstrap_phase.value`
    and the `bootstrap_failure_reason.value if ... else None` expression with the
    enum objects themselves (`bootstrap_phase`, `bootstrap_failure_reason`). Leave
    `"status": provider_status` as a string — `ComputeProviderInstanceRecord.status`
    is declared `str` (`packages/database/src/database/repositories/compute.py:113`),
    so that one is correct. Then split the shared dict: `records.create(...)` at
    `service.py:4001` wants a JSON-shaped mapping, `model_copy(update=...)` at
    `:4004` wants Python-typed values. Build the Python-typed dict and let the
    create branch derive JSON from it, rather than writing one dict that satisfies
    neither annotation honestly. Apply the same split at `service.py:4240-4263`.
  - **Risk**: low and contained to two functions. The declared annotation
    `dict[str, JsonValue | datetime]` currently type-checks only because
    `StringEnum` subclasses `str`; tightening it may surface pyright complaints at
    adjacent keys.
  - **Acceptance**: `uv run pytest packages/compute -q -W error::UserWarning` → 86
    passed, 0 failed (observed). Confirmed independently of ERR-01: this change
    alone took the same command from 5 failed to 86 passed.
  - **Depends on**: none (independent of ERR-01; both are wanted — ERR-03 fixes the
    source, ERR-01 prevents the next one)

- [ ] **ERR-04** Delete the three no-op revalidation calls
  - **Files**: `packages/database/src/database/repositories/compute.py:523`, `packages/database/src/database/repositories/compute.py:545`, `packages/compute/src/compute/service.py:2085`
  - **Change**: replace `self.upsert(ComputePoolRecord.model_validate(updated))` with
    `self.upsert(updated)` at both `compute.py` sites, and
    `repository.upsert(ComputePoolRecord.model_validate(current))` with
    `repository.upsert(current)` at `service.py:2085`. These pass an existing
    `ComputePoolRecord` instance to `model_validate`, which Pydantic v2 returns
    unchanged and unvalidated. ERR-01 provides the real guard inside `upsert`.
    Also delete the now-redundant explicit guard at
    `packages/database/src/database/repositories/compute.py:381`
    (`record = ComputePoolRecord.model_validate(record.model_dump(mode="python"))`)
    — but only after ERR-01 lands, because the `is not` immutability comparison at
    `compute.py:385` depends on it.
  - **Risk**: removing `compute.py:381` before ERR-01 would make the capacity-owner
    immutability check at `compute.py:383-388` compare a raw `str` against an enum
    with `is not`, raising spurious `ConflictError("compute pool capacity owner is
    immutable")`. Strict ordering.
  - **Acceptance**: `uv run pytest packages/compute packages/database -q -W error::UserWarning`
    passes. Grep proves the pattern is gone repo-wide:
    `grep -rnE "model_validate\((updated|record|model|current|existing)\)" packages apps --include="*.py" | grep -v /tests/`
    returns no `ComputePoolRecord` hits.
  - **Depends on**: ERR-01

- [ ] **ERR-05** Log the cause in the `DomainError` sink
  - **Files**: `apps/api/src/api/fastapi_app.py:219-224`
  - **Change**: mirror the unexpected-error handler at `fastapi_app.py:242-256`.
    Generate/read a request id, call `logger.exception(...)` with `exc_info=exc` so
    the `__cause__` chain is written, return the id on the response (header and
    body), and keep `exc.message` as `detail`. Log at `warning` for 4xx and `error`
    for 5xx-mapped domain errors — do not log 404s at error level. This alone makes
    all 84 chained domain raises debuggable without a remote cycle.
  - **Risk**: log volume on high-frequency 404/409 paths. Mitigate with level, not
    with sampling or suppression. No behaviour change for clients beyond an added
    id field.
  - **Acceptance**: with the API running locally, request a workspace-scoped
    resource that does not exist and one that conflicts; observe in the server log
    a `logger.exception` record carrying the originating traceback and the same
    request id returned to the client. Specifically: trigger the
    `UpstreamUnavailableError` at `packages/storage/src/storage/service.py:465` and
    confirm the underlying object-store exception appears in the log, where today
    only `"object completeness check failed: <bucket>/<key>"` is visible.
  - **Depends on**: none

- [ ] **ERR-06** Give `DomainError` and `ErrorResponse` a stable code
  - **Files**: `packages/shared/src/shared/errors.py:4-9`, `packages/shared/src/shared/http/errors.py:10-13`, `apps/api/src/api/fastapi_app.py:219-224`, `packages/lazycloud/src/lazycloud/cli/components/errors.py:90-137`, `apps/web/src/lib/api/schemas/` (Zod)
  - **Change**: add `code: str` to `DomainError.__init__` (defaulting to a value
    derived from the subclass, so existing raises stay valid) and `code: str` to
    `ErrorResponse`. Populate it in the sink from ERR-05. Extend the CLI's
    `normalize_exception` classifiers to branch on the code rather than on
    substring matching of the message (`errors.py:100` currently lowercases and
    joins messages to guess). Update the hand-written Zod schema in the same
    change, per CLAUDE.md.
  - **Risk**: **this is a public contract change touching five owners at once** —
    shared contracts, API sink, SDK, CLI renderer, and web schemas. Every non-2xx
    JSON body gains a field. Additive, so existing consumers keep working, but it
    cannot be split across changes without leaving the Zod schema out of sync.
    Scope honestly: this is the largest item in the track. See Open questions.
  - **Acceptance**: `lazycloud` CLI renders a typed error for a capacity-limit
    rejection where it previously printed a generic message; the same request over
    HTTP returns `{"detail": ..., "code": "capacity_limit_reached", ...}`; the
    dashboard renders it without a schema-validation error.
  - **Depends on**: ERR-05

- [ ] **ERR-07** Stop flattening `ManagedComputeLaunchError` into a string, and stop returning 500 for a quota hit
  - **Files**: `packages/compute/src/compute/service.py:195-205` (definition), `packages/compute/src/compute/service.py:2751`, `packages/compute/src/compute/service.py:1675`, and the 12 raise sites (`service.py:1703, 1706, 1904, 1916, 1947, 1953, 2015, 2772, 2786, 2922, 2963, 3563, 4334`)
  - **Change**: `ManagedComputeLaunchError` is a `RuntimeError` carrying a
    structured `code` — the repo already built the richer error type it needs,
    privately. Make it a `DomainError` subclass and pass its `code` through the
    field added in ERR-06. Then delete the two lossy conversions:
    `service.py:2751` (`raise ConflictError(f"{exc.code}: {exc}") from exc`
    concatenates a machine-readable code into prose) and `service.py:1675`
    (`raise UpstreamUnavailableError(str(exc)) from exc` **misclassifies a capacity
    limit as a 503 upstream outage**). Map each `code` to a status:
    `capacity_limit_reached` → 409, `provider_unavailable` → 503,
    `no_compatible_capacity` → 409.
  - **Risk**: any raise site not reached by a mapping falls through to
    `fastapi_app.py:242` and still returns 500 — which is today's behaviour, so
    this cannot regress, but an incomplete mapping silently keeps the bug. Enumerate
    all `code=` literals before mapping.
  - **Acceptance**: set a workspace `max_cpu_instances` below its current desired
    machines and attempt a launch; observe **409 with `code="capacity_limit_reached"`**
    where today the response is a 500 with `"internal server error (request id: ...)"`.
    Confirm no `ManagedComputeLaunchError` reaches `fastapi_app.py:242`:
    `grep -rn "ManagedComputeLaunchError" packages apps --include="*.py" | grep -v /tests/`
    shows every raise site reachable by a mapped handler.
  - **Depends on**: ERR-06

- [ ] **ERR-08** Record why a lease was lost instead of only that it was
  - **Files**: `packages/scheduler/src/scheduler/capacity_reservations.py:751-794`
  - **Change**: three bare `except Exception: lease_lost.set()` handlers at
    `capacity_reservations.py:759`, `:783`, `:790` make a Redis outage
    indistinguishable from a genuine lease steal on a fencing path. Capture the
    exception into a local `lease_loss_cause`, log it with `LOGGER.exception`, and
    raise `CapacityReservationLeaseLostError(...) from lease_loss_cause` at
    `:791-793` so the chain survives. Distinguish the three cases in the message:
    renewal failed, token mismatch (real steal), release failed.
  - **Risk**: the renewal handler runs on a daemon thread (`:766-770`); logging
    there must not raise. Behaviour is otherwise unchanged — the lease is still
    considered lost in all three cases.
  - **Acceptance**: with a local Redis, kill the Redis connection mid-mutation and
    observe the raised `CapacityReservationLeaseLostError` carrying a `__cause__`
    naming the connection failure, plus a logged traceback distinguishing it from a
    token-mismatch steal.
  - **Depends on**: none

- [ ] **ERR-09** Give the shell compensation path a reason
  - **Files**: `packages/execution/src/execution/shells/service.py:296-400`, `packages/execution/src/execution/shells/service.py:596-630`
  - **Change**: five bare handlers (`:302`, `:380`, `:393`, `:603`, `:627`) discard
    every cause on a cleanup path and return
    `ShellTicketCompensationStatus.Failed` with nothing attached. Add a `reason:
    str` to `ShellTicketCompensationResult`, populate it from the caught exception,
    and log with `LOGGER.exception` — this file has no logger today and needs one.
    Cleanup obligations that fail silently are the ones that leak containers.
  - **Risk**: `ShellTicketCompensationResult` is consumed by the shells service and
    its callers; adding a field is additive. Confirm no exhaustive match on the
    dataclass shape.
  - **Acceptance**: force a container stop failure and observe the compensation
    result carrying the underlying error text, plus a logged traceback naming the
    container id. Today both are absent.
  - **Depends on**: none

- [ ] **ERR-10** Collapse three at-limit conventions into one
  - **Files**: `packages/compute/src/compute/service.py:1009-1019` (`AtLimit` typed result), `packages/compute/src/compute/service.py:2313-2315` (`ConflictError`), `packages/compute/src/compute/service.py:2014-2017` (`ManagedComputeLaunchError`)
  - **Change**: the same user-facing condition — "you are at your configured
    capacity limit" — produces a typed result, a 409, and a 500 depending on which
    entry point the caller used. Choose the typed result
    (`CapacityAcquisitionStatus.AtLimit`) as the internal representation, since it
    is the only one that carries the numbers, and map it once at the service
    boundary to a 409 with `code="capacity_limit_reached"`. Delete the other two
    representations of the same condition.
  - **Risk**: **highest-coupling item in the track.** These three sites sit in the
    capacity-acquisition code CAP also edits. Sequence against CAP or fold this
    into CAP's work — do not land concurrently.
  - **Acceptance**: hit the limit through all three entry points (public scale,
    pooled acquisition, workspace policy reconcile) and observe the same status,
    the same `code`, and a message naming the limit and the current desired count.
  - **Depends on**: ERR-07, CAP-\* (sequencing)

- [ ] **ERR-11** Add `bootstrap_failure_detail` beside the failure enum
  - **Files**: `packages/database/src/database/repositories/compute.py:129-131`, `packages/shared/src/shared/http/compute_policy.py:104`, `packages/compute/src/compute/policy.py:77`, `packages/compute/src/compute/service.py:5081-5086`, `apps/api/src/api/server/routers/resource_api/compute_policy.py:154`, `apps/web/src/lib/api/schemas/compute.ts:238-247`
  - **Change**: **do not change `MachineBootstrapFailureReason`**
    (`packages/shared/src/shared/compute_enrollment.py:55-66`) — the enum is a
    correct closed set, and `packages/compute/src/compute/service.py:5104-5143`
    classifies genuinely, returning `WorkerReadinessFailed` when it has a better
    signal and `BootstrapTimedOut` only when it does not. The defect is that the
    record has no free-text companion. Add `bootstrap_failure_detail: str = ""` to
    `ComputeProviderInstanceRecord`, carry it through `ComputeInstanceView` and the
    HTTP response model, and populate it at `service.py:5081-5086`, where a real
    message is already constructed (`"machine did not produce an available worker
    before the bootstrap phase deadline"`) and then discarded. Update the Zod
    schema in the same change.
  - **Risk**: additive field on a persisted record; existing rows validate with the
    default. Web schema must land together per CLAUDE.md.
  - **Acceptance**: `lazycloud` CLI listing compute instances shows a failed node
    with both `bootstrap_timed_out` **and** the detail string. Today
    `packages/lazycloud/src/lazycloud/cli/resources.py:503-504` can only print the
    enum value.
  - **Depends on**: none. **BOOT-\* must land after this** to populate the detail
    from the bootstrap classifier.

- [ ] **ERR-12** Give `disable-worker` a reason field
  - **Files**: `packages/worker/src/worker/repository_payloads.py:123-124` (`WorkerIdRequest`), `apps/api/src/api/server/routers/worker_repository.py:290-297`, `apps/api/src/api/server/worker_repository_service.py:823-825`, `packages/scheduler/src/scheduler/state.py:773`, `packages/scheduler/src/scheduler/workers.py:29`
  - **Change**: `POST /worker-repository/disable-worker` carries only `worker_id`.
    The caller always knows why. Introduce a dedicated request model (do not widen
    `WorkerIdRequest` — it is shared by other routes) with `worker_id` plus a
    required `reason: str`, thread it through the service and the
    `SchedulerWorkerRepository.disable_worker` protocol, and persist it on the
    worker record so an operator can read why a node was taken out.
  - **Risk**: `disable_worker` appears in a `Protocol`
    (`packages/scheduler/src/scheduler/workers.py:29`) with multiple
    implementations; all must change together or the protocol check fails. Route
    is worker-authenticated, so this is not a public SDK contract.
  - **Acceptance**: disable a worker through the real route and read the reason
    back on the worker record. **I own the contract and the route; CAP owns wiring
    the callers** at `packages/worker/src/worker/worker_lifecycle.py:318` and
    `packages/gateway/src/gateway/service.py:1016`.
  - **Depends on**: none. **CAP-\* depends on this.**

- [ ] **ERR-13** Move compute-policy editing server-side
  - **Files**: `packages/lazycloud/src/lazycloud/cli/resources.py:589-635`, `packages/shared/src/shared/http/compute_policy.py:22-25`, `apps/api/src/api/server/routers/resource_api/compute_policy.py:44-61`
  - **Change**: the CLI fetches the current policy and reconstructs a whole
    `AwsWorkspaceComputePolicy` from ten stored values plus one flag
    (`resources.py:595`). A cross-field invariant
    (`packages/shared/src/shared/compute_policy.py:107-108`,
    `min_cpu_workers <= initial_cpu_workers <= max_cpu_instances`) therefore fails
    as a raw `pydantic.ValidationError` inside the CLI process — never reaching
    `HttpApiError` and never touching the CLI's typed renderer. Over HTTP the same
    edit yields a clean 422. Fix the asymmetry by accepting a partial update
    server-side: make the fields of the `aws` block optional on the update request,
    merge against the stored record inside the service, and validate once there.
    The CLI then sends only what the user typed.
  - **Risk**: changes the semantics of `PUT /policy` from full-replace to merge.
    That is a public contract change — decide whether it stays `PUT` (replace) with
    a new `PATCH`, or becomes a merge. See Open questions.
  - **Acceptance**: `lazycloud ... --max-cpu 1` against a workspace whose stored
    `initial_cpu_workers` is 2 returns a rendered CLI error naming the conflicting
    fields and their stored values, with a non-zero exit code — not a
    `pydantic.ValidationError` traceback.
  - **Depends on**: ERR-06 (for the typed code on the rendered error)

- [ ] **ERR-14** Stop billing a workspace that zeroed its CPU capacity
  - **Files**: `packages/compute/src/compute/policy.py:119-125` (`_aws_capacity_is_zero`), `packages/compute/src/compute/policy.py:132-144`
  - **Change**: capacity is released only when `min_cpu_workers`,
    `initial_cpu_workers`, `max_cpu_instances` **and** `max_gpu_instances` are all
    zero. `max_gpu_instances` defaults to 2
    (`packages/shared/src/shared/compute_policy.py:89`), so a workspace that zeroes
    every CPU knob it was told about keeps paying. Compounding this, the cross-field
    validator forces three edits to zero CPU at all. Split the predicate: evaluate
    CPU and GPU capacity independently and release each when its own knobs are
    zero. The comment at `policy.py:135-139` records that an adjacent version of
    this bug already cost a workspace money.
  - **Risk**: **cost-affecting and irreversible in the release direction** —
    releasing capacity terminates machines. Verify the GPU path separately before
    landing; a wrong predicate here destroys running workloads rather than merely
    billing for idle ones.
  - **Acceptance**: on a workspace with default `max_gpu_instances=2`, zero the
    three CPU knobs and observe CPU capacity released and billing stopped, with GPU
    capacity untouched. Then zero the GPU knob and observe GPU release.
  - **Depends on**: none

- [ ] **ERR-15** Split `WorkerSettings`
  - **Files**: `apps/container-worker/src/container_worker_app/production.py:232-732`
  - **Change**: one `BaseSettings` class holds **69 fields across 500 lines** — 31%
    of the repository's entire 220-field configuration surface, against a median of
    ~7 for the other 32 settings classes. Split by the boundary each group
    configures (container runtime, image/registry, storage and cache, networking,
    telemetry), each with its own `env_prefix` following the existing
    `ENV_PREFIX`-per-package convention. Compose them on the worker rather than
    inheriting.
  - **Risk**: environment variable names must not change, or every deployment
    breaks. Preserve each existing name exactly; the split is about ownership, not
    renaming. Verify against `deploy/` and `docker/` before landing.
  - **Acceptance**: the container-worker starts from an unchanged environment file
    and reports identical resolved configuration; `grep -r LAZYCLOUD_ deploy docker`
    shows no name changed.
  - **Depends on**: none

- [ ] **ERR-16** Extract provider reconciliation out of `ComputeService`
  - **Files**: `packages/compute/src/compute/service.py:287-5254` (`ComputeService`, **4,968 lines, 90 methods**)
  - **Change**: this one class is the generator of the inconsistencies in this
    track — both `model_copy` drift sites, all three at-limit conventions, and the
    code-flattening conversions live here. Extract the provider-reconciliation
    seam into its own module: `_reconcile_provider_machines` (162 ln),
    `_sync_pooled_instances` (151), `_apply_pooled_snapshot` (121),
    `_record_failed_launch_cleanup` (85), `_terminate_provider_record` (84),
    `_retire_provider_pool_machines` (82), `_reclaim_pooled_bootstrap_failures` (82),
    `reconcile_provider_capacity` (61), `_record_provider_instance` (56),
    `_terminate_overdue_stale_machines` (52), `_persist_zero_capacity_repair` (50),
    `_provider_bootstrap_failure_to_reclaim` (~45). **~1,100 lines of 4,968** —
    honest size; this is a fifth of the class, not a rewrite of it. The seam is
    real: these methods share the `ComputeProviderInstanceRecord` lifecycle and
    talk to provider snapshots, and nothing else in the class does.
  - **Risk**: they currently reach `self` for the database context, repositories and
    the reclaim settings; extraction requires passing those explicitly, which is the
    point but is also the work. **Do not attempt concurrently with ERR-10 or CAP's
    capacity-seam extraction** — two splits of a 4,968-line class will conflict
    irreconcilably.
  - **Acceptance**: `packages/compute/src/compute/service.py` drops below ~3,900
    lines; `uv run pytest packages/compute -q -W error::UserWarning` passes
    unchanged; no behaviour change intended, so no new test is added (Test Decision
    Gate: this is a refactor proven by existing authoritative coverage).
  - **Depends on**: ERR-03, ERR-10; sequenced against CAP-\*

- [ ] **ERR-17** Delete `_resolve_in_session`
  - **Files**: `packages/compute/src/compute/policy.py:420-469`
  - **Change**: delete the method. A repo-wide grep across `.py` and `.md`
    (excluding `.venv`/`node_modules`) returns only its own definition line — no
    callers, no tests, no dynamic dispatch.
  - **Risk**: none identified. If a later grep finds a caller, it was added after
    this plan; re-verify before deleting.
  - **Acceptance**: `grep -rn "_resolve_in_session" . --include="*.py" --include="*.md" | grep -v node_modules | grep -v "\.venv"`
    returns nothing; `uv run pytest packages/compute -q` passes.
  - **Depends on**: none

## Deletions

| File or symbol | Lines | Why safe to delete | What replaces it |
|---|---|---|---|
| `packages/compute/src/compute/policy.py` `_resolve_in_session` | 420-469 (50) | Repo-wide grep over `.py`/`.md` returns only the definition. No callers, no tests, no dynamic dispatch. | Nothing. Dead since introduction. |
| `packages/database/src/database/repositories/compute.py:523` `ComputePoolRecord.model_validate(updated)` | 523 | Input is already a `ComputePoolRecord`; Pydantic v2 returns it unvalidated (`revalidate_instances="never"`, verified). Provides zero protection. | `ERR-01` guard inside `_TableRecordStore._upsert`. |
| `packages/database/src/database/repositories/compute.py:545` same call | 545 | As above. | `ERR-01`. |
| `packages/compute/src/compute/service.py:2085` same call | 2085 | As above. | `ERR-01`. |
| `packages/database/src/database/repositories/compute.py:381` explicit revalidation | 381 | Becomes redundant once `_upsert` revalidates universally. **Delete only after ERR-01** — the `is not` immutability check at `compute.py:385` depends on it. | `ERR-01`. |
| `packages/compute/src/compute/service.py:2751` `raise ConflictError(f"{exc.code}: {exc}")` | 2751 | Concatenates a machine-readable code into prose because `DomainError` had no field for it. | `ERR-06` code field + `ERR-07` status mapping. |
| `packages/compute/src/compute/service.py:1675` `raise UpstreamUnavailableError(str(exc))` | 1675 | Misclassifies a capacity-limit condition as a 503 upstream outage. | `ERR-07` code→status mapping (409). |
| Two of the three at-limit representations | `service.py:2313-2315`, `service.py:2014-2017` | Same user-facing condition expressed three ways with three different HTTP outcomes. | `ERR-10`: single `CapacityAcquisitionStatus.AtLimit` mapped once at the boundary. |

Not deleted, deliberately: `MachineBootstrapFailureReason`
(`packages/shared/src/shared/compute_enrollment.py:55-66`). The enum is a correct
closed set and its classifier genuinely discriminates. It gains a sibling detail
field (ERR-11), not new members.

## The 194 BLE001 sites

`ruff BLE001` encodes exactly the rule this track wants. Probed truth table:

| handler body | BLE001 |
|---|---|
| `raise` | pass |
| `raise X(...) from exc` | pass |
| `logger.exception(...)` | pass |
| `return f"failed: {exc}"` | **flag** |
| `return f"failed: {type(exc).__name__}"` | **flag** |
| `return None` | **flag** |

CLAUDE.md forbids exclusions and per-file ignores, so there is no on-ramp: the
rule goes on only when every site is clean. **Real size: 194 production sites
plus 7 in tests = 201.** The reference fix already exists in the tree —
`packages/scheduler/src/scheduler/containers.py:658-675` re-raises nothing,
calls `LOGGER.exception(...)` with the container and capacity-owner ids, and
carries `f"...: {type(exc).__name__}: {exc}"` into the caller's contract. Copy
that shape. Note 91 files containing broad handlers have **no logger at all**
(253 handlers); those need a module logger added first.

Ordered smallest-first so the pattern is settled before the expensive packages:

- [ ] **ERR-20** `packages/foundation` — 1 site
- [ ] **ERR-21** `packages/providers/aws` — 1 site
- [ ] **ERR-22** `packages/observability` — 2 sites
- [ ] **ERR-23** `packages/storage` — 2 sites
- [ ] **ERR-24** `packages/storage-client` — 2 sites
- [ ] **ERR-25** `apps/agent` — 4 sites
- [ ] **ERR-26** `packages/gateway` — 4 sites
- [ ] **ERR-27** `packages/lazycloud` — 4 sites (4 in `abstractions/serve.py`)
- [ ] **ERR-28** `packages/control` — 7 sites
- [ ] **ERR-29** `apps/container-worker` — 8 sites (4 `production.py`, 4 `main.py`)
- [ ] **ERR-30** `packages/worker-repository` — 8 sites (5 in `image_build_scheduler_execution.py`)
- [ ] **ERR-31** `packages/runner` — 9 sites
- [ ] **ERR-32** `packages/compute` — 12 sites (all in `service.py`) — **after ERR-16**
- [ ] **ERR-33** `packages/images` — 13 sites (5 in `service.py`)
- [ ] **ERR-34** `packages/execution` — 14 sites (7 in `shells/service.py`) — **overlaps ERR-09**
- [ ] **ERR-35** `apps/api` — 18 sites (8 in `server/services.py`)
- [ ] **ERR-36** `packages/scheduler` — 22 sites (14 in `containers.py`, 4 in `capacity_reservations.py`) — **overlaps ERR-08**
- [ ] **ERR-37** `packages/worker` — 63 sites (**26 in `container_service/service.py`**, 6 `image_build_execution.py`, 5 `scheduler_requests.py`) — a third of the whole backlog in one package; budget for it separately
- [ ] **ERR-38** Enable the rule
  - **Files**: `pyproject.toml:153-155`
  - **Change**: `select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]` → add `"BLE"`,
    `"TRY400"`, `"LOG"`, `"G"`. **Do not add `"TRY"` wholesale**: `TRY003` fires
    1821 times and is pure style. `TRY400` and `LOG` are already clean; `G` has 1
    violation.
  - **Acceptance**: `uv run ruff check packages apps` exits 0.
  - **Depends on**: ERR-20 … ERR-37

Each of ERR-20…ERR-37 is done when
`uv run ruff check --select BLE001 <package>` exits 0 **and** each converted
handler either re-raises or logs with a traceback — not when the count merely
drops. Where a handler is genuinely best-effort (e.g.
`packages/worker/src/worker/container_metrics.py:121`, which documents that a
usage read must never take the container down), the fix is still to log at debug
with `exc_info`, never to narrow the rule.

## Track acceptance

```bash
# 1. Persistence: no record can hold a value its annotation forbids.
uv run pytest packages apps -q -p no:randomly          # with filterwarnings=error::UserWarning
# expected: 0 failed. Baseline before ERR-01/03: 5 failed in packages/compute.

# 2. The gate bites. Restore `.value` at compute/service.py:3985, then:
uv run pytest packages/compute/tests/test_pooled_capacity.py -q
# expected: PydanticSerializationUnexpectedValue failures. Revert.

# 3. Failure semantics: the rule is machine-checked.
uv run ruff check packages apps
# expected: exit 0 with BLE, TRY400, LOG, G selected.

# 4. No decorative revalidation survives.
grep -rnE "model_validate\((updated|record|model|current|existing)\)" packages apps \
  --include="*.py" | grep -v /tests/
# expected: no ComputePoolRecord hits.

# 5. Dead code gone.
grep -rn "_resolve_in_session" . --include="*.py" --include="*.md" \
  | grep -v node_modules | grep -v "\.venv"
# expected: no output.
```

Live observations, none of which a unit test can stand in for:

- A workspace at its capacity limit receives **409 with
  `code="capacity_limit_reached"`**, not a 500 naming a request id (ERR-07).
- A failed node in the CLI shows `bootstrap_timed_out` **and** a detail string
  naming what actually happened (ERR-11, with BOOT-\* populating it).
- A `DomainError` raised in a service writes its `__cause__` traceback to the
  server log under the request id returned to the client (ERR-05). Verify with the
  `UpstreamUnavailableError` at `packages/storage/src/storage/service.py:465`,
  where today only `"object completeness check failed: <bucket>/<key>"` is
  visible.
- Zeroing the CPU knobs on a default-GPU workspace stops CPU billing (ERR-14).
- `lazycloud ... --max-cpu 1` against a conflicting stored policy prints a
  rendered error, not a `pydantic.ValidationError` traceback (ERR-13).

No new automated tests are proposed. Every item above is proven either by
existing authoritative coverage (ERR-01/03/04/16/17 — the drift is already
exercised by `packages/compute/tests/test_pooled_capacity.py`), by a lint gate
(ERR-20…ERR-38), or by a live observation that a unit test cannot substitute for
(ERR-05/07/11/13/14). Per the Test Decision Gate, none of these clears all four
conditions for a new test.

## Open questions for the owner

1. **`DomainError` / `ErrorResponse` code field (ERR-06) — scope honestly.**
   This is the largest item and touches five owners in a single change, because
   CLAUDE.md requires Pydantic contracts, SDK/CLI consumers and hand-written web
   Zod schemas to stay synchronized: `packages/shared/src/shared/errors.py`,
   `packages/shared/src/shared/http/errors.py`, `apps/api/src/api/fastapi_app.py`,
   `packages/lazycloud/src/lazycloud/cli/components/errors.py`, and
   `apps/web/src/lib/api/schemas/`. It is additive, so no consumer breaks, but it
   cannot be split without leaving the web schema out of sync mid-flight.
   **Decide: (a) do it now as one cross-owner change, (b) do ERR-05 only (log the
   cause — recovers all debuggability, no contract change) and defer the code
   field, or (c) drop the code field and keep classifying by message substring as
   `errors.py:100` does today.** Option (b) captures most of the observed pain for
   a fraction of the scope; ERR-07's 409-instead-of-500 fix needs only a status
   mapping, not the code field on the wire.

2. **`PUT /policy` semantics (ERR-13).** Fixing the CLI's client-side policy
   reconstruction means the server must accept a partial update. Should `PUT`
   become a merge (changing existing semantics), or should a new `PATCH
   /api/v1/.../policy` be added and `PUT` left as full-replace? The second is
   cleaner but adds a route.

3. **`ComputeService` decomposition ownership (ERR-16).** I propose extracting
   only the ~1,100-line provider-reconciliation seam. The ~1,500-line
   capacity-acquisition seam is CAP's territory. **If CAP is also splitting this
   class, these must be sequenced, not parallelised** — and someone must own the
   order. I have assumed ERR-16 goes second.

4. **The 63 `packages/worker` BLE001 sites (ERR-37).** A third of the backlog,
   with 26 in a single file (`container_service/service.py`), which returns
   `ok=False, error_msg=str(exc)` from a sandbox RPC surface. That file may want
   restructuring rather than 26 individual handler fixes — but that is a larger
   decision than this track should make unilaterally.

5. **Scope of `filterwarnings` (ERR-02).** I verified `packages/` is clean under
   `error::UserWarning` with ERR-01 applied. I did **not** run `apps/` or root
   `tests/` under it. If a dependency proves noisy, narrow by warning class —
   never by file, and never with a per-test override.
