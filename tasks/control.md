# Control packet

Parity sections from tasks/parity.md: "Apps and deployments" except serve,
export of endpoints and endpoint URLs; the remaining "Functions and tasks"
items; `lazycloud logs` and `lazycloud container list/stop/attach` from "Logs,
events and metrics"; `lazycloud app export` for functions.

Migration `migrations/0003_control.sql`. Protobuf fields 60-69.

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
- `capacity_limit` and `provisioning_compute` are defined but not produced
  until compute provisions hosts and enforces limits.

## Plan

1. Contract: OpenAPI paths and schemas, runner `dependency` frame, proto
   fields 60-61, migration 0003. Regenerate bindings.
2. Go: control (apps, deployments, plan, prune, run releases), execution
   (task list, stop, rerun, dependencies, pending reasons, containers list and
   stop, logs by deployment and container, retirement of deleted releases),
   API handlers, supervisor and agent pass-through.
3. Python: SDK task and call API, terminal output, CLI commands, runner
   dependency resolution, app export for functions.
4. Integrated check on a private local stack; measurements.

## Progress

- [ ] Contract and migration
- [ ] Go owners and API
- [ ] Python SDK and CLI
- [ ] App export
- [ ] Integrated run and measurements

## Evidence
