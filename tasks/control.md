# Control packet

Parity sections from tasks/parity.md: "Apps and deployments" except serve,
export of endpoints and endpoint URLs; the remaining "Functions and tasks"
items; `lazycloud logs` and `lazycloud container list/stop/attach` from "Logs,
events and metrics"; `lazycloud app export` for functions.

Schema: Control in `migrations/0001_schema.sql`. Protobuf fields 60-69.

## Decisions

- A deployment is a workload (one function of an app). Its versions are its
  releases. `deployment list` shows one row per workload with its active
  version; `deployment ... NAME-vN` names version N of NAME when no workload
  is called `NAME-vN`. `deployment start NAME-vN` makes version N active
  again, which replaces the reference's starting an older version's own
  deployment row.
- Apps and workloads are soft-deleted: `state = 'deleted'` frees the name,
  keeps task history, and execution planning retires the deleted releases:
  pending containers stop, the rest drain, queued and running tasks are
  cancelled. Pause drains and cancels queued tasks but lets running ones
  finish, as slice 1 already did. Prune deletes the workloads an app no longer
  declares, as the reference removed their versions.
- Run releases: `.remote()`, `.spawn()`, `.map()` and `lazycloud run` from a
  laptop run the working tree. The SDK uploads the source and calls
  `prepareFunctionRelease`, which reuses any release of the workload with the
  same spec digest (so unchanged code reaches the warm deployed containers)
  or inserts an unversioned release that is never active. Submits name it by
  `release_id`. Inside a container, calls go to the active release.
- Dependencies: a `FunctionCall` pickles as the persistent id
  `("function_call", task_id)`; the submit lists the ids per input in
  `depends_on`. A dependent waits as `queued` with `unmet_dependencies > 0`,
  which claims and planning ignore. Upstream success decrements the count; a
  failed or cancelled upstream fails every transitive dependent with
  `dependency_failed`. The claim carries upstream results, the supervisor
  sends them as `dependency` frames before `invoke`, and the runner resolves
  the persistent ids. A task's input plus its dependency results are capped
  at 48 MiB so one RunAttempt stays within the 64 MiB link limit.
- Pending reasons are derived in SQL from durable rows on every read of a
  queued task: unmet dependencies, a future `available_at` after an attempt
  (retry), a starting container of the release (starting_container), an
  unplaced pending container (capacity_unavailable), a ready container whose
  slots are all busy (capacity_busy), otherwise queued.
- Parent and root linkage: submits take `parent_task_id`; the server derives
  `root_task_id` from the parent row. The claim carries `root_task_id` so the
  runner sets `current_task_id()` and `current_root_task_id()`.
- Every collection is cursor-paginated (`limit`, `cursor`, `next_cursor`) and
  reads an index whose leading columns match its filter.

## Intentional differences

- `deployment list` lists workloads, not every version; versions come from
  `GET .../deployments/{id}/versions`. Only the active release takes
  deployed calls, so listing replaced versions as active would mislead.
- `app list` columns are name, state, workloads and created; apps have no
  version or public flag in the new model.
- `container list` shows the stop reason in the exit column; containers
  report how they stopped, not a process exit code. `container attach` ends
  with the stop reason and exits nonzero for anything but a requested stop.
- `TaskResult.exit_code` is `None`: tasks fail with a typed failure, not an
  exit code.
- `capacity_limit` and `provisioning_compute` come from the compute
  packet's capacity controller.
- `deployment stop NAME-vN` is refused unless N is the active version, and
  `delete NAME-vN` is always refused: stop and delete act on the workload.
- `task result` shows the decoded value, as slice 1 did.
- `app export --json` lists each workload's `release_id` instead of an
  invoke URL, and an app without deployed functions, endpoints or ASGI apps
  is an error. Endpoints and ASGI apps call the URL that follows the active
  release, so a redeploy without contract changes keeps working; the export
  version still changes with the release ids, as for functions.
- A deploy may list no function only with prune, which deletes every
  deployed function, as the reference's prune of an empty app did.
- Upstream tasks must be in the submitting workspace.
- Status words follow the public API in the SDK and CLI too: `queued`,
  `running`, `succeeded`, `failed`, `cancelled`. A retry is `queued` with
  pending reason `retry`, and a timeout is `failed` with failure kind
  `timeout`; there is no `expired`. docs/concepts/tasks-and-logs.mdx says so.
- Short ids in the terminal (Task step, Runtime step, pending card, the
  "Connecting" step) and the dashboard show the last 8 characters. Ids are
  UUIDv7, whose leading characters are a timestamp that ids made together
  share, so the reference's first 8 would not tell tasks apart.
- `deploy()` on an app, function, endpoint or pod returns the API's
  `Deployment` (app, releases, pruned, removed_versions) instead of
  `AppDeployResult` or `DeployStubResponse`, and takes no `external_url=`:
  URLs come from the server. docs/concepts/pods.mdx reads
  `releases[0].url`.
- Waits long-poll the server, so `poll_interval_seconds` is gone from
  `FunctionCall.result/get/gather` and `Task.result/wait/async_wait`.
  `FunctionCall(task)` wraps a `Task`; the snapshot attributes `complete`,
  `error`, `exit_code`, `status` and `workspace_id` are gone (read
  `call.task.get()` or `call.result()`). A wait past its timeout raises
  `TimeoutError`, a failure re-raises the remote exception or
  `RemoteTaskError` (`TaskOperationError` is gone), `logs()` returns
  `LogEntry` (`data`), and `cancel()`, `Task.get()` and `Task.view()` return
  the API `Task`. `TaskPendingProgress.for_reason` is gone with the
  generated model.
- `spawn_map` submits up to 1,000 inputs per request instead of one request
  per input, 8 at a time; a failed later batch raises `MapSubmissionError`
  with the calls already admitted. Ctrl-C during `map` cancels the tasks not
  yet yielded.
- `map` waits for each result without the reference's client-side deadline
  of one task timeout, which reported a task still queued behind others as
  failed; the server enforces each task's timeout.
- A dropped connection during `.remote()` resumes following for up to 600 s
  before it cancels the task; the reference cancelled at once.
- Redeploying an unchanged workload keeps its active version (the spec
  digest matches); docs/concepts/workflow.mdx says so.
- `logs --json` prints a list of log entries (`id`, `task_id`, `attempt`,
  `stream`, `data`, `time`) instead of `{"data": [...], "next": ""}`, and
  `--task-id`/`--container-id` take ids.
- `container list` names each container by its workload and shows the
  container states `pending`, `starting`, `ready`, `draining`, `stopped`.
- `app show` shows name, state, workloads and created, as `app list` does.
- `Deployment.invoke_url()` returns the workload's URL, `url_type="stub"`
  the active release's own host and `port=` a pod's port; `url_type` is a
  string, `"deployment"` or `"stub"`, instead of `GatewayUrlKind`.
- An app deploys in one request, so it lands whole or not at all; up to 4
  distinct images build at once and the source uploads once. The reference
  ran 4 per-resource deploys at a time and could leave some finished after
  a failure.
- `App.plan()` and `deploy --diff --json` return the API `DeploymentPlan`
  (`app`, `prune`, `items`) without a snapshot, since deploy and prune are
  one request.
- `Function.prepare()` returns the working-tree release id instead of a stub
  id and `Function.serve()` returns a `Preview`. `Function` has no `stub_id`,
  `endpoint`, `token`, `timeout`, `control_client` or `deployment_client`:
  releases replace stubs and the client comes from the profile.

## Plan

1. Contract: OpenAPI paths and schemas, runner `dependency` frame, proto
   fields 60-61, schema. Regenerate bindings.
2. Go: control (apps, deployments, plan, prune, run releases), execution
   (task list, stop, rerun, dependencies, pending reasons, containers list and
   stop, logs by deployment and container, retirement of deleted releases),
   API handlers, supervisor and agent pass-through.
3. Python: SDK task and call API, terminal output, CLI commands, runner
   dependency resolution, app export for functions.
4. Integrated check on a private local stack; measurements.

## Progress

- [x] Contract and migration
- [x] Go owners and API
- [x] Python SDK and CLI
- [x] App export
- [x] Integrated run and measurements

## Evidence

Owner tests against PostgreSQL: control `lifecycle_test.go` (app pause,
resume, delete and name reuse; deployment stop, start on a version,
versions paging, delete; plan actions; working-tree release reuse; prune of
everything), execution `dependencies_test.go` (dependents wait, receive the
upstream result in the claim, fail transitively, oversized inputs fail),
`task_views_test.go` (pending reasons, task paging and filters, stop, rerun,
parent and root, container stop, deletion cancels running work,
working-tree release of a stopped workload, logs by workload and container
with tail), `list_cost_test.go`; API `control_test.go` (authorization,
routing, validation). Python: `test_cli_control.py`, `test_app_export.py`,
the task and dependency tests in `test_sdk_platform_api.py`, runner
`test_dependency_frames_resolve_upstream_results_for_the_next_invoke`.

Integrated run on a private stack (server, scheduler, agent with runc, one
24-CPU host, PostgreSQL 18, Garage), SDK and CLI from this branch:

| Scenario | Result |
| --- | --- |
| Cold `.remote()` from a working tree, nothing deployed | 1.04 s, Image/Source/Runtime/Task rows |
| Warm `.remote()` (100 calls, output off) | p50 10.3 ms, p95 13.3 ms (slice 1: 9-14, 12-18) |
| `double.spawn(double.spawn(1)).get()` | p50 13.8 ms, p95 17.8 ms |
| Nested FunctionCalls in a list argument | resolved to results in the container |
| Pending card behind a busy container | `capacity_busy` after 5 s; callback saw starting_container, capacity_busy, None |
| `container stop` of a running container | draining at once, slot killed, stopped 0.12 s later, task retried on a new container |
| `app delete` with running and queued tasks | both cancelled, no live container 0.22 s later |
| Ctrl-C during `lazycloud run` | task cancelled |
| List tasks / app tasks / containers (page 100) | p50 1.3 / 1.6 / 0.8 ms over HTTP |

`list_cost_test.go` with 10,000 finished tasks: a 101-row task page reads
311 buffers in 0.2 ms, the live-container page 7 buffers, pending facts 7,
with no sequential scan of tasks, attempts or containers.

Gaps: calls from inside a container need the workload-runtime container API
for credentials; endpoint invoke URLs, `serve`, ASGI export and container
checkpoints belong to other packets. A status filter on the task listing
reads the workspace's recent index and filters, which grows with history
between matches. Log readers order lines by (writer xid, id) and hold back
lines newer than the oldest running writing transaction, so a long-running
write transaction anywhere in the cluster delays log delivery until it ends;
reads without follow wait at most 2 s for it.
