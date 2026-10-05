# Batch task wait

## Scope

`map()` and `spawn_map()` collect many results in a few round trips instead
of two per task. Owns:

- `contracts/openapi.yaml`: `POST /v1/workspaces/{workspace}/tasks/wait`,
  operation `waitTasks`, and its request and response schemas. Regenerate
  every binding (Go, Python profiles, `bun run apigen`).
- `internal/execution`: `WaitTasks` and its query in `queries/tasks.sql` (or
  the file the task queries live in).
- `internal/api`: the handler.
- `python/lazycloud/src/lazycloud/abstractions/function.py` (`map`,
  `spawn_map`), `python/lazycloud/src/lazycloud/session/task.py`, the client
  call, and their tests.

No migration. Stay off compute, scheduling, the agent and web.

## Plan

The contract:

- Request: `task_ids`, 1 to 1000 unique task IDs; `wait_seconds`, 0 to 60.
- Response: `tasks`, the requested tasks that are finished, each as the
  existing task view plus its result inline when the result fits the inline
  budget. Unfinished tasks are left out.
- It returns as soon as at least one requested task is finished, or when
  `wait_seconds` passes with none finished (an empty list, not an error).
- Every ID must name a task in the caller's workspace. Otherwise the call
  fails with the existing not-found error naming the first unknown ID, so a
  client never waits forever on a task it cannot see.
- Inline budget: a result inlines when it is at most 256 KiB and the
  response stays under 4 MiB; any other finished task comes back marked as
  not inlined, and the client fetches it with `getTaskResult`. Choose the
  exact field shape from the existing task and payload schemas.

Server:

- `Execution.WaitTasks(ctx, listener, workspace, ids, wait)` subscribes to the
  task-finish channel the single-task wait uses before its first read, so no
  finish between the read and the wait is lost. One query per wake reads the
  finished subset (`id = any($ids)` in the workspace, terminal status), with
  the results it inlines. No per-task queries.
- The handler validates the bounds, authorizes the workspace like
  `GetTask`, and maps the typed not-found error.

SDK:

- `map()` submits as today (batched), then loops: `waitTasks` on the
  still-pending IDs in groups of at most 1000, records what came back,
  fetches non-inlined results, and yields values in input order as soon as
  the next one in order is known. A failed task still yields `None`.
  Ctrl-C still cancels the tasks not yet yielded.
- `spawn_map()` callers that wait on many calls get the same path through
  one helper; single `.remote()` and `FunctionCall.get()` keep the
  single-task path.

## Evidence to record

- Owner tests against the real test Postgres: returns only finished tasks;
  wakes on a finish that lands during the wait; returns empty at the
  deadline; refuses a task from another workspace; inlines within the budget
  and marks the rest.
- SDK test proving order and `None` for failures.
- Before and after on a local stack: `map()` of 100 and of 1000 trivial
  calls, wall time and API request count. The prod number comes after Ship
  with `/tmp/lcb2/lc_fn.py`.

## Progress

## Intentional differences

## Gaps and unverified boundaries
