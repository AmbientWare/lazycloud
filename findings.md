# Full codebase bloat and quality investigation

Date: 2026-07-18
LazyCloud revision audited: `a147028`
Comparison repository: `../beta9`
Work tracking: unresolved findings are queued in `board/todo.md`, active implementation lives in
`board/loop.md`, and completed outcomes move to `board/finished.md`. Keep this checklist current
until every finding is checked; the boards organize delivery but do not replace the checklist.

## Executive verdict

LazyCloud is larger than Beta9, but it is not simply an overgrown Python rewrite and it should not be redesigned around Beta9's implementation just to reduce line count.

The conservative production-source comparison is approximately:

| Surface | LazyCloud | Beta9 | Interpretation |
| --- | ---: | ---: | --- |
| Application and package code, excluding test/build/dependency directories | 200,503 code lines | 136,404 code lines excluding Go protobuf output | LazyCloud is about 47% larger. The Beta9 number still includes about 5,258 generated Python client lines, so it is not a pure handwritten comparison. |
| LazyCloud web application alone | 23,116 code lines | No comparable dashboard in this repository | Removing the dashboard leaves LazyCloud about 30% larger. |
| Runner | about 2,375 lines | about 2,118 lines | Effectively comparable. |
| Scheduler plus workload autoscaling | about 13,292 lines | about 16,166 handwritten lines | LazyCloud is smaller in this comparable slice. |
| Container worker package plus app | about 36,805 handwritten lines | about 24,154 handwritten lines | LazyCloud is larger, but Beta9 also checks in about 16,754 generated worker/container protobuf lines. |
| Compute, providers, and agent | about 16,857 lines | about 9,694 lines | LazyCloud is materially larger, partly because its connected AWS and customer-compute lifecycle is broader. |
| Deployment/operations/acceptance support | about 28,606 code lines | about 14,866 code lines | LazyCloud directly owns a turnkey Compose stack, a much fuller Helm chart, and more production acceptance. |

Beta9's simpler appearance mainly comes from four choices:

1. It generates gRPC/protobuf clients and servers. Approximately 67,378 physical lines of `.pb.go` are checked in, plus generated Python clients and generated mocks. These lines are visually hidden from normal design work but still exist in the repository and build.
2. Its Helm chart delegates most Kubernetes rendering to the `bjw-s/common` library chart and several dependency charts. LazyCloud owns its templates and validation schema directly.
3. It has no comparable dashboard source in this repository.
4. It uses fewer, more concrete objects and larger Go files. Go syntax is somewhat more compact at these boundaries, but language choice is not the primary explanation.

The important conclusion is therefore mixed:

- Much of LazyCloud's extra size is justified product and operational scope: the dashboard, richer HTTP contracts, connected AWS lifecycle, tenant isolation, audit/event streams, full Compose parity, explicit Helm resources, and cleanup evidence.
- There is also serious, removable bloat. Some of it is not merely untidy: it creates security, correctness, and false-acceptance risks.
- The current event-driven dispatch, independent worker heartbeat, and SSE feedback design recorded
  in `board/finished.md` is appropriate. Switching transports or rewriting in Go would not address
  the concrete problems found here.
- The best production direction is to preserve the real workflow and delete alternate/fake paths, make each resource have one mutation authority, make package dependencies acyclic, and consolidate repeated boundary plumbing.

## Resolution checklist

This remains the authoritative 29-item completion checklist. Update a box only after the production
owner, applicable integration/public workflow, and cleanup evidence are accepted. The three boards
own task flow and concise results so this report does not become a command diary.

- [x] F-01 — Remove control-plane execution of workspace commands.
- [x] F-02 — Remove or replace the privileged generic cron execution plane.
- [x] F-03 — Replace fake generic-provider lifecycle acceptance with real lifecycle truth.
- [x] F-04 — Make bootstrap and device authorization atomic.
- [x] F-05 — Replace long-lived WebSocket bearer query tokens with single-use tickets.
- [x] F-06 — Establish one app lifecycle mutation authority.
- [x] F-07 — Remove resource CRUD/list duplication from `/gateway`.
- [x] F-08 — Break the `scheduler`/`execution` package dependency cycle.
- [x] F-09 — Remove fake-Redis atomicity fallbacks from production.
- [x] F-10 — Stop composing divergent API and operator-CLI backends.
- [x] F-11 — Remove stale worker `auto`/`direct` repository modes.
- [x] F-12 — Remove or fully integrate the unowned CacheFS/raw/gRPC surface.
- [x] F-13 — Remove the test-only Kubernetes manifest deployment architecture.
- [x] F-14 — Establish one registry-credential parsing owner.
- [ ] F-15 — Make provider support claims match live production evidence. Tracked by T-005.
- [ ] F-16 — Split concentrated owners along durable state-machine boundaries. Tracked by T-007.
- [x] F-17 — Reduce protocol and dependency-injection plumbing without weakening boundaries.
- [x] F-18 — Remove fail-only, forwarding-only, and test-only production abstractions.
- [x] F-19 — Establish one SDK HTTP transport.
- [ ] F-20 — Remove relational-column/JSON dual persistence truth. Tracked by T-001 and T-012.
- [x] F-21 — Make workspace lookup read-only.
- [x] F-22 — Replace the mutable lazy API service locator with lifespan composition.
- [x] F-23 — Retire `shared.models` through latest-only focused contracts.
- [x] F-24 — Make type enforcement match repository policy.
- [x] F-25 — Establish one canonical worker/deployment configuration model.
- [x] F-26 — Strengthen Pydantic/Zod/SDK contract parity evidence.
- [x] F-27 — Consolidate public/operator CLI bootstrap and presentation plumbing.
- [x] F-28 — Split web settings controllers and reusable typed e2e fixtures where valuable.
  Tracked by T-008.
- [x] F-29 — Consolidate stable acceptance-harness plumbing without hiding workflow evidence.

## Highest-risk findings

### F-01 — Critical: workspace commands run inside the credentialed API process

This is the most urgent problem found.

Evidence:

- `apps/api/src/api/server/routers/resource_api/tasks.py:97-118` exposes `POST /api/v1/tasks` to an ordinary workspace write token.
- The request accepts an arbitrary string or argument vector through `shared.http.tasks.CommandRequest`.
- `packages/execution/src/execution/tasks.py:592-618` calls `subprocess.Popen` inside the API process.
- The child environment is `env={**os.environ, **(env or {})}`, so it inherits control-plane environment and credentials.
- The HTTP request is synchronous and `timeout_seconds` is optional.
- Command reruns in `packages/execution/src/execution/task_rerun.py:83-90` use the same direct process path.
- No public SDK caller or production route acceptance test was found for the create-command route.
- Beta9 has no corresponding path that turns a tenant command into a control-plane subprocess; workload execution goes through its scheduler/worker/runtime path.

Impact:

- A workspace writer can execute in the control-plane trust domain rather than an isolated user container.
- The path bypasses scheduler admission, OCI isolation, the container worker, runner, usage accounting, normal cleanup, and resource limits.
- A long-running or hostile command can consume the API process and inspect its environment.

Recommendation:

- Delete `POST /api/v1/tasks` for raw commands and command-task reruns if raw command tasks are de-scoped.
- If this is a real product capability, model it as a normal isolated container workload and send it through the same scheduler, worker, runner, event, result, cancellation, and cleanup path as other user code.
- Add a route-contract test that proves the API process never spawns tenant commands.

This is bad production architecture, not an acceptable Python-versus-Go difference.

### F-02 — Critical: generic cron has a second unsafe execution plane and is broken across workspaces

Evidence:

- `packages/scheduler/src/scheduler/service.py:1058-1083` publishes arbitrary `command` or dynamically loadable `handler` payloads for non-function cron jobs.
- `apps/worker/src/worker_app/loop.py:313-338` executes the command or imports and calls the handler inside the credentialed worker service process.
- The scheduler publishes with `workspace=cron_job.workspace_id`.
- The worker calls `consume(queue)` and `ack(..., queue=...)` without a workspace.
- `packages/execution/src/execution/collections/service.py:39-57` defaults both operations to workspace `"default"`.
- Consequently, the production worker cannot consume generic cron messages belonging to non-default workspaces.
- The scheduler test proves tenant enqueue, then manually consumes from that tenant. It does not traverse the production worker.
- The extra `apps/worker` deployable costs about 845 production lines and 97 direct test lines before its Compose, Helm, database, event, and task plumbing.
- Read-only live inspection found one healthy worker process but two durable worker rows still marked `running`; restart did not reconcile the predecessor.

Impact:

- Tenant behavior is incorrect outside the default workspace.
- User code runs in another privileged service process instead of the canonical isolated runtime.
- Worker presence can remain falsely live after restart.
- The code duplicates the function-cron path, which already uses canonical container execution.

Recommendation:

- Keep function cron and remove generic command/handler cron plus `apps/worker` unless the product explicitly requires a separate job-runner architecture.
- If retained, redesign it as isolated execution, pass workspace identity explicitly at every queue operation, and use leased/TTL-backed worker presence with cleanup.

### F-03 — High: the provider lifecycle release gate is a fake success path

Evidence:

- `packages/providers/generic/src/provider_generic/provider.py:66-157` “launches” by appending a machine to process-local `settings.machines` and immediately returns it as active. It provisions nothing.
- `packages/provider-clients/src/provider_clients/factory.py:153-173` exposes this adapter through the production provider registry.
- `packages/compute/src/compute/service.py:867-929` treats the fabricated result as launched provider capacity and persists it.
- `e2e/deployment/provider-lifecycle-smoke.sh` defaults to `e2e/provider/validate.sh:7-35`.
- That smoke instantiates the in-memory adapter, asserts its local list, and prints `provider lifecycle passed` without a provider, node registration, worker pickup, workload, termination, or leak audit.
- Live provider mode checks only AWS or Hetzner credential health; it does not prove launch and teardown.
- Beta9's generic provider explicitly returns `ProviderNotImplemented` in `../beta9/pkg/providers/generic.go:34-45`.

Impact:

- A required release gate reports production provider lifecycle success when no lifecycle happened.
- The same fake adapter is reachable from production configuration.
- Tests can validate a path that cannot work after a process restart and does not create capacity.

Recommendation:

- Delete generic auto-launch and use durable agent enrollment as the only bring-your-own-node workflow.
- Rename any retained local test to a provider-contract unit test; do not call it lifecycle acceptance.
- Make provider lifecycle acceptance use a real supported provider, await node enrollment and worker readiness, run a public workload, delete the capacity, and prove no provider or platform resources remain.
- When credentials are unavailable, record the live-acceptance blocker instead of passing a substitute path.

### F-04 — High: bootstrap and device authorization contain transaction races

Evidence:

- `packages/identity/src/identity/auth.py:240-267` checks that token count is zero and then creates workspace, token, and primary-token association across separate transactions.
- There is no database singleton constraint for bootstrap authority. Concurrent bootstrap calls can both observe zero tokens and mint administrators.
- `packages/identity/src/identity/device_auth.py:114-148` reads an approved device row, deletes it, commits, and creates a token in a later transaction.
- Concurrent claimers can both read approval before either deletion commits and both mint credentials.
- Approval/denial is another read-modify-upsert without a conditional state transition.
- Existing tests prove sequential single-use behavior, not concurrent PostgreSQL behavior.

Recommendation:

- Put bootstrap behind a database-enforced singleton or transaction/advisory lock.
- Consume an approved device code with an atomic conditional `DELETE ... RETURNING` or state transition and create the token in the same transaction.
- Make approve/deny conditional on `pending`.
- Add concurrent PostgreSQL integration tests.

### F-05 — High: browser WebSocket URLs contain the long-lived bearer token

Evidence:

- `apps/api/src/api/server/dependencies.py:117-127` accepts authorization or token query parameters for WebSockets.
- `apps/web/src/lib/queries/shells.ts:23-31` puts the stored bearer token in `?token=...`.
- URLs can appear in proxy/access logs, browser diagnostics, traces, and copied session data.
- Beta9 also accepts query credentials, so Beta9 is not a safer model here.

Recommendation:

- Add an authenticated POST that mints a short-lived, audience-bound, single-use WebSocket ticket.
- Atomically consume the ticket during upgrade.
- Remove long-lived bearer query support after the web client migrates.

## High-confidence architectural bloat

### F-06 — App lifecycle has two mutation authorities with behavior drift

Evidence:

- `packages/control/src/control/apps.py:38-155` implements app create/get/list.
- `packages/control/src/control/service.py:868-982` independently implements the same operations.
- The latter checks `ArtifactCleanupRepository.assert_stub_available()` while `AppService.create()` does not.
- API/gateway routes use `AppService`, while deployment registration still receives `ControlPlaneService`.

Recommendation:

- Make `AppService` the sole app lifecycle owner and inject artifact availability there.
- Narrow `ControlPlaneService` to the state machines it actually owns.
- Update deployment registration to depend on narrow app/stub ports and delete the duplicate methods.

### F-07 — `/gateway` duplicates canonical `/api/v1` resource routes

Evidence:

- Gateway exposes listing/CRUD/control for tasks, deployments, pools, machines, tokens, and workers in `packages/gateway/src/gateway/service.py:958-1774` and the API gateway routers.
- The same durable resources have canonical routes under `/api/v1`.
- `packages/shared/src/shared/http/gateway.py` adds roughly 726 lines of parallel resource contracts.
- `packages/lazycloud/src/lazycloud/clients/gateway/control.py` adds roughly 372 lines of parallel client code.
- `TaskClient` uses gateway contracts for list/cancel but `/api/v1` contracts for detail, putting two task representations in one public client.
- Repository guidance already says `/gateway/*` is for RPC-style deploy/resolve/container/task control, while resources belong under `/api/v1`.

Recommendation:

- Make `/api/v1` the only list/CRUD/inspection surface.
- Migrate SDK/operator callers, then delete overlapping gateway models, methods, and routes.
- Retain true RPC and runtime callbacks: deploy/resolve, runner start/end/log, streaming, agent runtime, and similar control operations.

### F-08 — `scheduler` and `execution` form the workspace's only dependency cycle

Evidence:

- `packages/scheduler/pyproject.toml` depends on `execution`.
- `packages/execution/pyproject.toml` depends on `scheduler`.
- The cycle is real: scheduler autoscaling imports execution services and workload controllers, while execution container/pod/shell services import scheduler state and services.
- Dependency analysis of all 40 workspace distributions found one strongly connected component: `execution <-> scheduler`.
- The 88 architecture tests pass because none asserts that the workspace dependency graph is acyclic.
- Beta9 is acyclic in this area; Go would reject a package import cycle.

Recommendation:

- Freeze one dependency direction.
- Put stable scheduling request/result/address contracts in a boundary-safe owner.
- Let execution define a scheduling port implemented by scheduler, or let scheduler receive execution callbacks, but do not keep both concrete directions.
- Compose cross-owner reconcilers in `apps/scheduler`.
- Add an acyclic workspace-dependency test.

### F-09 — Scheduler production code contains fake-Redis atomicity fallbacks

Evidence:

- `packages/scheduler/src/scheduler/state.py:268` defines `_LOCAL_REDIS_ATOMIC_LOCK`.
- Thirteen production methods check whether `redis.client.eval` exists and choose between Lua and a manual multi-command path under that process-local lock.
- Fourteen affected functions/nested functions span about 624 full-function lines.
- `tests/redis_fakes.py` does not implement `eval`, so the large scheduler repository and worker-repository integration suites exercise the non-production branch.
- A Python lock cannot emulate atomicity across API and scheduler processes.

Recommendation:

- Delete the capability fallback from production.
- Run repository integration against a script-capable Redis service.
- Keep pure decision tests independent of Redis.

This is both bloat and false test parity.

### F-10 — API and operator CLI compose different copies of the backend

Evidence:

- `ApiServices` has 61 fields and an approximately 330-line factory in `apps/api/src/api/server/services.py`.
- `CliServices` has 40 fields and independently rebuilds most of the graph in `apps/cli/src/cli/services.py`.
- Thirty-six service field names overlap.
- The copies have drifted: API uses configured production filesystem ownership; CLI hardcodes `LocalVolumeFilesystem`; API closes owned Redis clients; CLI has no equivalent close lifecycle and creates additional clients.
- The CLI calls backend services directly, so operator workflows can differ from the production API path.
- Beta9's normal CLI behavior primarily uses its API/gRPC surface.

Recommendation:

- Make normal `lazycloud-admin` resource operations use authenticated admin HTTP APIs.
- Reserve direct service/database composition for narrowly scoped bootstrap, migration, or recovery commands.
- Where local construction is genuinely required, expose owner-level factories with explicit close contexts rather than duplicating an application graph.

### F-11 — Worker `auto` and `direct` repository modes are stale transition architecture

Evidence:

- Modes remain in `packages/worker/src/worker/repository_client.py:131-134`; canonical configuration defaults to `Auto`.
- `build_production_worker_process_services` is a 423-line split in `apps/container-worker/src/container_worker_app/production.py`.
- One branch uses the canonical authenticated HTTP repository client; the other composes database, Redis, S3, execution, images, observability, scheduler, and storage directly into a remote worker.
- Compose, Helm, and private-agent planning all select client mode.
- Helm still advertises all three modes in its JSON schema.

Recommendation:

- Confirm whether any supported production deployment still requires direct mode.
- If not, freeze client-only behavior and delete `Auto`, `Direct`, direct sinks, branch-specific dependencies, schema values, and tests.

### F-12 — CacheFS/raw/gRPC is a large unintegrated parity surface

Evidence:

- `packages/cache/src/cache/filesystem.py` is 978 lines with 53 public symbols. Static production-reference analysis found only `CacheFsMetadata` referenced outside the module, and that is used by payload/state for orphaned endpoints.
- `packages/cache/src/cache/protocol.py` has 30 of 44 public symbols without an external production reference, including raw-read, mux, page, prefetch, and client transport contracts.
- `packages/cache/src/cache/cluster.py` has 21 of 30 public symbols unused outside the file, including discovery, host-map, gRPC, and advertise planning.
- The worker-repository router exposes 69 routes; `WorkerRepositoryHttpClient` calls 44. The 25 orphaned routes include all six CacheFS endpoints and several cache registration/locking/reconcile operations.
- The active cache server registers directly in Redis and serves whole objects over HTTP. No production FUSE mount/client was found.
- Beta9's CacheFS is actually mounted and called from its cache client.

Recommendation:

- Delete the unintegrated CacheFS/raw/gRPC plans, payloads, state, configuration, and orphaned routes.
- Reopen them only as a full production workflow with a real mount client, scheduler/worker integration, acceptance, and cleanup.

This is one of the clearest examples of parity code being written without parity behavior.

### F-13 — Kubernetes has a second, test-only deployment architecture

Evidence:

- `packages/providers/kubernetes/src/provider_kubernetes/provider.py` is 1,017 lines.
- Production imports only the Kubernetes container-worker deployment scaler and focused settings.
- `deployment_manifests()` and its handwritten resource tree occupy roughly lines 257-977, about 721 lines.
- No production or e2e caller uses the generator; only deployment/unit tests call it.
- Real Kubernetes e2e installs `deploy/charts/lazycloud` with Helm.
- Helm is the accepted deployment owner.

Recommendation:

- Delete the alternate manifest generator and its tests.
- Retain the replica scaler and the minimal typed settings it needs.

### F-14 — Registry credential parsing has two owners

Evidence:

- `packages/shared/src/shared/image_building/credentials.py` owns public credential-name resolution, registry host parsing, secret-name normalization, and ECR parsing.
- `packages/images/src/images/building/credentials.py` repeats that same security-sensitive logic before adding backend credential planning.
- Public SDK and AWS provider callers use the shared copy; image-service callers use the images copy.

Impact:

- Registry recognition or credential changes can diverge between public authoring, provider behavior, and image builds.
- This violates the single canonical owner rule in a secret-handling boundary.

Recommendation:

- Make the shared module the canonical protocol-neutral parser.
- Have the images owner import those primitives and own only backend credential detection, marshalling, and execution plans.

### F-15 — Provider breadth includes speculative or misleading adapters

Evidence:

- AWS is substantial and documented as live-verified; its added code is largely justified.
- Hetzner, OCI, Lambda Labs, Vast, Crusoe, Hydra, and Shadeform are documented as implemented but not live-verified.
- Several provider clients are 200-300-line near-copies with mocked HTTP lifecycle tests.
- Hydra defaults to `https://api.hydra.example`, so its default cannot represent a real production API.
- Live validation supports only AWS and Hetzner credential health, not full marketplace lifecycle.
- The thin Crusoe, Hydra, Shadeform, and Vast files have high structural similarity, but their real APIs and lifecycle semantics should differ.

Recommendation:

- Audit each provider against an actual supported provider account and contract before advertising it as implemented.
- For speculative adapters, fail explicitly as not implemented and remove their production registry entries until accepted.
- Share only a small authenticated HTTP lifecycle harness among retained providers; keep provider-specific request, response, status, idempotency, and cleanup logic typed and separate.

Implementation checkpoint (2026-07-21):

- AWS is now the only registered production machine provider. The former speculative adapters are
  absent from tracked source and dependency metadata, unsupported names fail explicitly, and the
  limitations reference no longer describes them as implemented.
- The connected-AWS release gate now requires an exact disposable account, region, hourly cost
  ceiling, dedicated workspace, public gateway, and validated tailnet credentials before mutation. It
  always attempts destructive cleanup and checks scoped durable, coordination, local-secret, AWS,
  and tailnet absence instead of accepting credential health or an in-memory lifecycle. Tailnet
  safety uses objective credential scopes, tag separation, live routing, and device cleanup rather
  than a self-attested policy-acknowledgement flag.
- F-15 remains open until that gate completes against the authorized live account and proves no
  provider or platform resources leaked.

## Medium-severity maintainability findings

### F-16 — Owner concentration is high

Not every large file is wrong—Beta9 also has 2,000-3,000-line worker and repository files—but LazyCloud concentrates multiple state machines in several owners:

| Owner | Concentration |
| --- | --- |
| `packages/scheduler/src/scheduler/state.py` | 3,484 lines; worker membership/leases, backlog claims, dispatch, queues, image locks, container lifecycle, routes, cancellation, indexes, and concurrency repair. |
| `packages/gateway/src/gateway/service.py` | 3,143 lines; runtime callbacks plus overlapping resource administration. |
| `packages/compute/src/compute/service.py` | A roughly 2,174-line class covering pool/provider/machine/worker CRUD, launch, reconciliation, billing, retirement, and compensation. |
| `apps/api/src/api/server/worker_repository_service.py` | 2,453 lines and 69 route operations. |
| `apps/container-worker/src/container_worker_app/production.py` | 2,387 lines with a 423-line assembly function. |
| `packages/scheduler/src/scheduler/autoscaling.py` | 2,052 lines across endpoint, queue, and pod controllers. |
| `packages/control/src/control/service.py` | 1,777 lines with overlapping app ownership. |
| `packages/operations/src/operations/management.py` | 1,604 lines across independent operational resources. |

Recommendation:

- Split by existing durable aggregate/state machine and transaction boundary, not by arbitrary line count.
- Do not preserve a catch-all façade that simply forwards every old method.
- First remove alternate/dead paths; then split the remaining owner so the correct boundaries are clearer.

### F-17 — Protocol and dependency-injection volume has crossed into plumbing bloat

Evidence:

- The production Python tree defines roughly 497 `Protocol` classes.
- About 306 have zero or one method; about 384 have at most two.
- Worker plus container-worker alone has roughly 154 protocols; scheduler about 66; execution about 47.
- Comparable Beta9 areas have far fewer Go interfaces, though its concrete structs are more coupled.
- The workspace has 40 Python distributions and 231 internal dependency edges.
- `lazycloud-shared` has 38 dependents; `compute` has 22.
- `ApiServices.create` has about 51 parameters; other composition roots have 20 or more.

Counterevidence:

- Consumer-owned protocols provide valuable import isolation and testability.
- The 88 architecture tests demonstrate that many of these boundaries are intentional and working.

Recommendation:

- Keep protocols at actual process, external system, or replaceable owner boundaries.
- Combine one-method fragments that always travel together into cohesive owner capability ports.
- Use small immutable dependency bundles at composition roots, not a mutable service locator and not a mega-container passed through business code.
- Do not remove protocols wholesale; remove duplicate and fail-only contracts first.

### F-18 — Several repeated/fail-only abstractions add no behavior

Examples:

- The same scheduler container-state repository concept is declared separately in scheduler, worker, and execution.
- `SchedulerContainerAddressBook.client_for()` only raises that no transport factory is configured, while production always injects the real client factory.
- `RedisMapService` and `RedisSimpleQueueService` each forward nine of nine methods directly to repositories.
- `TaskService.submit_callable` has no caller; `submit_command` is test-only; its daemon-thread map is written but never joined.
- `coordination/hot_state.py` is test-only.
- Several storage and image-reconciliation helpers have no production reconciler/caller.

Recommendation:

- Require real dependencies explicitly.
- Put validation and decisions in services or expose the owning abstraction directly; do not retain one-for-one wrappers.
- Delete production APIs whose only consumers are tests unless the production workflow is implemented immediately.

### F-19 — HTTP transport is implemented three times and has already drifted

Evidence:

- `packages/shared/src/shared/http_transport.py` implements `HttpChannel`, JSON/SSE, TLS, and HTTP error conversion.
- `packages/lazycloud/src/lazycloud/client_handles.py:295-428` implements another raw urllib stack.
- `packages/lazycloud/src/lazycloud/abstractions/endpoint.py:1086-1203` implements a third.
- Generated endpoint handles raise `HttpApiError` on HTTP error responses; decorated endpoint requests return `EndpointResponse` for the same class of response.
- JSON/data precedence, URL merging, response URL handling, TLS, and error conversion differ.

Recommendation:

- Create one SDK-owned raw HTTP transport with a typed raw response and explicit error policy.
- Route generated handles and decorator endpoint requests through it.
- Keep control-plane JSON/SSE semantics distinct only where the protocol truly differs.

### F-20 — Relational columns and JSON payloads create dual persistence truth

Evidence:

- Tables inherit `PayloadMixin`.
- `packages/database/src/database/repositories/common.py:387-545` globally synchronizes model payloads into columns.
- `_column_values()` knows fields across unrelated aggregates and maps duplicated/special field names.
- Repositories query both typed columns and nested JSON.
- Migrations repeatedly rewrite JSON while adding relational indexes and columns.

Counterevidence:

- This is durable PostgreSQL, not a fake JSON store.
- Beta9's repository layer is larger and includes a roughly 3,005-line PostgreSQL backend; it is not automatically a better model.

Recommendation:

- Make relational columns canonical for identity, lifecycle, state, and query fields.
- Retain JSON only for flexible configuration/metadata.
- Give each aggregate an explicit row/domain mapper and migrate one aggregate at a time.

### F-21 — Lookup methods can mutate durable workspace state

Evidence:

- `packages/database/src/database/context.py:56-65` performs a read-only UUID lookup, but a missing name calls `ensure_workspace()` and creates it.
- `ControlPlaneService.get_workspace()` similarly ensures the default workspace.
- Many backend callers use this lookup helper.

Recommendation:

- Create the default workspace during explicit bootstrap/migration.
- Make lookup strictly read-only and raise `NotFoundError`.
- Keep explicit create/upsert as the only mutation authority.

### F-22 — The API root is a mutable lazy service locator

Evidence:

- `ApiServices` contains many optional mutable service slots.
- FastAPI dependencies repeatedly do `if service is None: construct; services.service = ...`.
- These synchronous dependencies may be entered concurrently in worker threads.
- Some lazy constructions allocate clients and runtime dependencies.
- A checker-only `TYPE_CHECKING` import hides part of the composition cycle.

Recommendation:

- Compose the complete immutable graph in application lifespan startup.
- Model disabled integrations as explicit typed capabilities.
- Preserve narrow domain protocols at use sites.

### F-23 — `shared.models` remains a large dependency-gravity center

Evidence:

- Repository guidance already labels `shared.models` migration debt.
- It contains 20 source files and about 4,136 physical lines.
- Approximately 375 production files import from `shared.models`; there are over 1,000 import/reference occurrences.
- `shared.http` is a newer focused contract surface, but SDK, service, repository, worker, and provider code still depend heavily on the older model center.

Recommendation:

- Do not perform a broad rename or create compatibility re-exports.
- Move one accepted boundary at a time into a focused `shared.http.*` or protocol-neutral module, update every caller, and delete the old owner immediately.
- Prevent new `shared.models` dependencies with an architecture test.

### F-24 — Type precision policy is stronger in prose than in enforcement

Evidence:

- `pyrightconfig.json` uses `typeCheckingMode: "basic"` and disables unknown argument/member/parameter/variable reports.
- Production has six explicit `type: ignore` suppressions:
  - `packages/gateway/src/gateway/container_transport.py:177`
  - `packages/worker/src/worker/container_client/scheduler.py:116`
  - `packages/execution/src/execution/tasks.py:469-470`
  - `packages/execution/src/execution/tasks.py:502-503`
- A heuristic scan found about 1,123 `Any` occurrences and 278 `object` annotations in production. Many are legitimate user-callable or external JSON boundaries, but many backend ports return broad values despite the interface cost.
- The six suppressions contradict the repository's non-negotiable typing rule.

Recommendation:

- Fix the socket, scheduler-client, and `jsonable` contracts directly.
- Incrementally enable stricter diagnostics for the whole tree without exclusions or checker-only adapters.
- Normalize untrusted data at Pydantic/typed boundary models, then use precise internal types.

Passing `basedpyright` currently proves the configured basic gate, not the stronger design policy.

### F-25 — Worker and deployment configuration is shadowed repeatedly

Evidence:

- `WorkerConfiguration` is about 180 lines with roughly 51 nested fields.
- `ProductionWorkerSettings` is about 590 lines with 86 flattened fields, 26 `resolved_*` properties, and 20 `AliasChoices`.
- CLI flags, two Compose worker definitions, Helm values, Helm schema, templates, and environment variables repeat the contract.
- Compose contains about 200 distinct `LAZYCLOUD_*` names; Helm templates contain about 147; roughly 149 overlap.
- Helm's container-worker global and pool override schemas repeat fields such as repository mode/timeout, keepalive, and spindown.

Counterevidence:

- Secret references, global defaults, per-pool overrides, and different deployment environments are legitimate.
- The Helm schema already uses shared definitions extensively; it is not wholly hand-duplicated.

Recommendation:

- Use one canonical nested worker configuration plus a small environment/secret overlay.
- Remove direct/auto mode before consolidating, because it causes much of the branch-specific configuration.
- Reuse one JSON-schema definition for global/pool worker settings and keep parity tests between Compose, Helm, and Pydantic defaults.

### F-26 — Manual contract mirroring is an accepted tax but needs stronger parity checks

Evidence:

- `shared.http` contains 36 Python files and about 4,816 physical lines.
- Web Zod schemas contain 28 files and about 1,967 physical lines.
- The public SDK has roughly 100 Python source files and about 22,896 physical lines.
- Beta9 authors 22 protobuf files and generates Go/Python clients, which moves much of this repetition out of handwritten code.
- Repository rules intentionally require hand-maintained Zod and prohibit OpenAPI generation.

Assessment:

- This is a deliberate REST/browser/readability tradeoff, not automatically bad programming.
- It becomes bad only when equivalent fields/statuses/defaults are updated independently without contract parity evidence.

Recommendation:

- Keep Pydantic and Zod hand-authored under the current architecture decision.
- Expand field/default/enum conformance tests across Pydantic, web schemas, and SDK response models.
- Do not switch to gRPC or OpenAPI generation solely to reduce checked-in source.

### F-27 — Public and operator CLI bootstrap/presentation code is duplicated

Evidence:

- Public and operator `components/context.py` are byte-for-byte identical.
- Both contain large exception-rendering and profile/token command plumbing.
- `profile_set`, `token_set`, startup normalization, error rendering, and common public command registration repeat.
- The operator CLI also mounts several public commands but composes backend services for others.

Recommendation:

- Keep public versus operator command authority separate.
- Put reusable presentation, profile persistence, error normalization, and public Typer subapps in the public/client-safe owner.
- Have the operator CLI mount those and add operator-only commands; never make the public package import backend services.

### F-28 — Web code is mostly justified, with several concentration and test-fixture hotspots

Assessment:

- The roughly 23,116-code-line dashboard is an extra product capability absent from the Beta9 repository, not comparative bloat.
- Its route/component/query/schema split is generally reasonable.
- It has 39 unit test files and 206 passing tests.

Hotspots:

- `ComputeSettings.tsx` is 825 lines and owns multiple cloud/policy/query/mutation/UI concerns.
- The settings route is 773 lines and owns workspace identity, token management, and deletion flows.
- `AwsConnectionDialog` is 546 lines with six lifecycle mutations and recovery state. Much of that is justified by the durable AWS workflow, but the controller state should be extracted from presentation.
- `apps/web/tests/e2e/ia.spec.ts` is 1,466 lines; its first roughly 870 lines build a large route-mocking server before 11 tests. This is valuable information-architecture coverage, but the typed mock API belongs in reusable fixtures.
- The generated `routeTree.gen.ts` is 434 lines and uses `@ts-nocheck`/`any`; it should remain classified as generated code, not hand-maintained type debt.

Recommendation:

- Extract owner hooks/controllers from the three settings hotspots.
- Create reusable typed Playwright API fixtures without hiding test-specific assertions.
- Do not split components merely to reduce line count.

Resolution (2026-07-21):

- The settings route fell from 773 lines to a 70-line composition root. Workspace identity, token
  lifecycle, AWS connection, compute policy, and shared workspace deletion now each have one
  state-machine/controller and exact query/cache owner; presentation-heavy modules were retained
  where line count alone did not justify another boundary.
- One-time token plaintext bypasses TanStack state, non-workspace credentials are managed/read-only,
  token issuance authority rejects workspace-to-admin escalation, and self credential mutation is
  conflict-safe. Strict workspace/token Zod contracts and canonical workspace-root query keys make
  deletion cache removal exact rather than predicate-based.
- The reusable strict Playwright API fixture now covers the focused settings ownership workflow
  without absorbing scenario assertions. All E2E TypeScript is in the normal compiler project, and
  strict desktop/mobile acceptance covers rename, tokens, deletion retry/navigation, sibling
  preservation, and interrupted-delete recovery. The large IA spec remains explicit because its
  scenario-specific route evidence is valuable; no split was made merely to reduce its line count.

### F-29 — Acceptance scripts contain repeated harness plumbing, but most volume is valuable

Evidence:

- Several e2e scripts are 1,000-1,900 lines and repeat HTTP, CLI subprocess, parsing, environment, redaction, polling, and cleanup helpers.
- `tests/architecture/test_e2e_helpers.py` is 1,635 lines testing much of that orchestration.
- Exact copy detection found only a few byte-equivalent helpers; most repetition is similar rather than identical because workflows retain different evidence and cleanup.
- The startup benchmark harness is smaller than Beta9's benchmark tree and is not a bloat concern.

Recommendation:

- Extract only stable process/HTTP/redaction/polling primitives into a typed e2e harness.
- Keep resource ownership, expected evidence, terminal outcomes, and cleanup explicit in each workflow.
- Fix the fake provider gate before trying to shorten acceptance code.

Resolution (2026-07-22):

- The complete 84-item acceptance audit deleted the monolithic harnesses, harness-unit tests, plan
  printers, command inventories, fake telemetry, private-store censuses, and wrapper-only
  entrypoints. The old root `e2e/` tree no longer exists.
- Fifty-three independently callable production scenarios now live under `tests/e2e/local/` and
  `tests/e2e/external/`; browser evidence remains with Playwright. Each scenario owns its public
  evidence and cleanup. Shared support is limited to live gating and bounded secret-safe process
  execution, with no workflow, polling, deployment, or teardown framework.
- Ordinary Pytest excludes opt-in scenarios, all 53 blocked module preflights pass, and canonical
  Compose readiness plus one real public Function invocation pass with cleanup. Connected-AWS
  acceptance is split into eight stages; its zero-cost preflight correctly refuses stale public
  compute state instead of bypassing it.

## Owner-by-owner disposition

| Owner/surface | Disposition | Main reason |
| --- | --- | --- |
| `shared` | Keep boundary-safe contracts; actively retire `shared.models`; consolidate credential parsing | Centrality is expected, but old model gravity and duplicate credentials are real debt. |
| Public `lazycloud` SDK/CLI | Mostly justified; consolidate transports and CLI plumbing | Rich typed authoring, handles, apps, realtime, and sandbox features explain size. |
| API composition/routes | Needs major correction | Unsafe command route, duplicate gateway resources, mutable composition, and long-lived WS query token. |
| Identity | Valuable security scope with urgent transaction fixes | Token hashing/scopes/audit are stronger than Beta9; concurrency authority is not atomic. |
| Control | Needs ownership cleanup | App lifecycle exists twice and the main service mixes aggregates. |
| Database | Durable and legitimate, but mapper design is over-generic | PostgreSQL ownership is correct; payload/column dual truth is not. |
| Operator CLI | Too much duplicated backend composition | Normal resource operations should traverse production HTTP authority. |
| Web | Legitimate additional product scope | Concentrated settings controllers and giant e2e fixture are secondary cleanup. |
| Execution | Needs security and boundary cleanup | Raw local command/callable paths, duplicated scheduler contracts, broad payload types. |
| Scheduler | Correct event-driven direction; repository implementation is overgrown | Package cycle and fake-Redis paths undermine otherwise strong durable dispatch. |
| Container worker | Core capability is justified; alternate direct mode is not | Client mode is the accepted production architecture. |
| Generic worker | Remove or redesign | Unsafe second execution plane, tenant bug, stale durable presence. |
| Runner | Healthy and comparable | Small, focused, and close to Beta9 in size. |
| Cache | Active HTTP/object cache is legitimate; parity surface is not | CacheFS/raw/gRPC has no mounted production client. |
| Images/storage | Mostly justified; consolidate credentials and dead reconciliation APIs | Real S3/volume/archive workflows explain scope. |
| Compute/AWS | Large but substantially justified | Durable customer cloud, connected account, billing, enrollment, and cleanup exceed Beta9's scope. |
| Marketplace providers | Support claims need proof | Several are structurally similar, mocked only, or have placeholder defaults. |
| Kubernetes provider | Keep scaler; remove manifest generator | Helm is the accepted deployment owner. |
| Agent/networking | Mostly justified | Enrollment, tailnet, telemetry, and provider-node security are real production features. |
| Observability/operations | Scope is justified; management façade is concentrated | Metrics, events, logs, and usage are product capability, not generic decoration. |
| Compose/Helm | Larger for good reasons, but configuration needs one source of defaults | LazyCloud owns a much fuller turnkey stack than Beta9's delegated chart. |
| Tests/e2e/benchmarks | Volume is generally healthy; semantic parity has critical gaps | Fake Redis, fake provider acceptance, and generic-cron coverage matter more than raw test count. |
| Docs | Useful and candid, except provider “implemented” claims need tightening | Limitations are documented, but placeholder/unverified adapters should not appear production-complete. |

## What should not be simplified away

The following are quality or capability, not bloat:

- The event-driven worker pickup stream and independent heartbeat/liveness design accepted in
  `board/finished.md`.
- Durable Redis dispatch, PostgreSQL lifecycle state, S3-compatible artifacts, and cleanup ownership.
- Separate public SDK, runner, and backend dependency boundaries.
- Small focused FastAPI router files; Beta9's lower file count often comes from much larger route files.
- Typed HTTP response models that prevent leaking internal persistence/secrets.
- Strong workspace scoping, token hashing, scopes, audit attribution, and authorization invalidation.
- Connected AWS account lifecycle, node identity proof, launch compensation, draining, revocation, and billing records.
- The dashboard and its validated API boundary.
- Production acceptance that exercises public CLI/SDK routes and proves cleanup.
- Tests as a category. Scheduler and worker test-to-production ratios are not unusually high relative to Beta9; the concern is when tests select a non-production path.
- Python as the implementation language. A rewrite would add migration risk while leaving ownership, fake paths, and duplicated workflows conceptually unchanged.

## Recommended remediation order

### Phase 0 — Contain execution and credential risk

1. Remove or disable raw command creation/rerun in the API.
2. Remove generic command/handler cron and the generic worker, or block it until isolated execution is designed.
3. Replace WebSocket bearer query parameters with single-use tickets.
4. Make bootstrap/device authorization atomic and add concurrency tests.

Acceptance:

- A workspace token cannot cause subprocess execution in API, scheduler, or service-worker processes.
- Every user-code path produces a scheduler/container/runner lifecycle and cleanup evidence.
- Concurrent auth tests prove exactly one bootstrap admin and one device-code claimant.

### Phase 1 — Restore truth in provider and cache acceptance

1. Remove generic in-memory launch from production and the release gate.
2. Make a real provider lifecycle with enrollment/workload/teardown the acceptance owner.
3. Mark or remove speculative providers; Hydra should fail explicitly rather than default to an example domain.
4. Delete unintegrated CacheFS/raw/gRPC surfaces and orphaned worker-repository routes.
5. Delete the test-only Kubernetes manifest generator.

Acceptance:

- The provider release row cannot pass without a real provider lifecycle and leak audit.
- No production adapter reports a resource active without durable/external evidence.
- Every remaining cache route has a production client and accepted workflow.

### Phase 2 — Establish one owner and one route per resource

1. Make `AppService` the sole app owner.
2. Move resource inspection/CRUD from `/gateway` to `/api/v1` callers and delete duplicates.
3. Move normal admin CLI operations to authenticated HTTP.
4. Make workspace lookup read-only.
5. Make API composition immutable at lifespan startup.

Acceptance:

- Architecture tests enumerate creator, mutator, reconciler, and deleter for each affected resource.
- SDK, CLI, API, service, repository, and cleanup all traverse the same owner.

### Phase 3 — Remove alternate execution architecture and dependency cycles

1. Freeze container workers to repository client mode and delete direct/auto branches.
2. Break the `scheduler <-> execution` package cycle and add a graph test.
3. Delete fake-Redis fallbacks and run script-backed repository tests.
4. Remove fail-only adapters and duplicate scheduler protocols.

Acceptance:

- Workspace package graph is acyclic.
- Production and repository integration use the same Redis atomicity.
- Remote worker package no longer depends on database/scheduler internals solely for direct mode.

### Phase 4 — Consolidate repeated boundary plumbing

1. Unify registry credential parsing.
2. Unify SDK HTTP transport.
3. Consolidate public/operator CLI presentation and subapp registration.
4. Replace worker settings overlays with one canonical nested config plus secret/environment overlay.
5. Add stronger Pydantic/Zod/SDK parity tests.
6. Extract stable e2e harness utilities.

### Phase 5 — Split the remaining monoliths by state machine

After dead and duplicate paths are gone, split scheduler state, gateway control, compute lifecycle, worker repository, and autoscaling along their actual ownership and transaction boundaries.

A conservative deletion opportunity from already identified dead/duplicate production surfaces is roughly 6,000-10,000 lines before broader consolidation. The more important gain is not the percentage of repository lines; it is removing entire false architectures, security paths, configuration branches, and mutation authorities from the mental model.

## Validation and counterevidence gathered

The audit used repository inventories, `cloc`, AST analysis, package dependency/SCC analysis, exact and structural duplication searches, production reference scans, direct route/service/repository/config review, targeted Beta9 workflow/test review, and read-only local Compose/database inspection.

Current validation results:

- `uv lock --check`: passed.
- Ruff check across apps/packages/tests/benchmarks/e2e/examples: passed.
- Ruff format check: 1,120 files already formatted.
- Basedpyright: 0 errors, 0 warnings, 0 notes under the current basic configuration.
- Architecture tests: 88 passed.
- Web ESLint: passed.
- Web unit tests: 39 files and 206 tests passed.
- Web TypeScript check: passed.
- Web production build and prerender: passed.

These passing gates are meaningful counterevidence: the repository is disciplined, formatted, import-isolated in many areas, well tested, and buildable. They do not cover the package cycle, transaction races, subprocess trust boundary, fake provider acceptance, fake-Redis branch parity, or unused production surfaces identified above.

## Limitations of this investigation

- No live AWS/Hetzner/marketplace resource was created or destroyed during this audit; provider claims beyond existing evidence remain unverified.
- The bootstrap/device races are source-level high-confidence findings but were not reproduced under concurrent PostgreSQL load.
- Static reference analysis can miss reflection or external consumers. Recommended deletions should still begin with call-site and public-contract confirmation.
- Line counts use different languages and generation models and are directional, not a quality score.
- The full Python test suite, Helm smoke, provider live lifecycle, and full browser production e2e were not rerun for this report. Focused architecture/static/web checks were run.
- No runtime profiler or coverage report was generated. This report evaluates architecture, ownership, production reachability, acceptance integrity, security boundaries, duplication, and maintainability—not CPU/memory hotspots.

## Final answer to the original question

Yes, LazyCloud has more code than Beta9. No, that is not mainly because Python is worse or because the whole design is overgrown.

The dashboard, richer production deployment, HTTP/browser contracts, customer compute, stronger tenant/security lifecycle, and checked-in operational evidence account for much of the legitimate difference. Beta9 also hides substantial complexity in generated protobuf output, third-party Helm rendering, and large concrete Go owners.

But there is real poor bloat in LazyCloud. The clearest examples are the direct control-plane command runner, generic privileged cron worker, fake generic-provider acceptance, fake-Redis production fallbacks, duplicate Kubernetes deployment generator, unintegrated CacheFS parity surface, stale direct worker mode, duplicate gateway resources, cyclic scheduler/execution ownership, and duplicate app/credential/transport owners.

The highest-quality simplification is not a rewrite. It is to delete every path that does not traverse the accepted production workflow, then make the remaining workflow have one contract, one durable owner, one reconciler, and one cleanup path.
