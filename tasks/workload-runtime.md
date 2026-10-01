# Workload runtime

Packet: container API, secrets, schedules, lifecycle hooks, `in_process`
slots and task callbacks (tasks/wave-2.md). Migration
`migrations/0004_workload_runtime.sql`; protobuf fields 10-19 in existing
host and container messages.

## Outcome

A deployed function calls the platform from inside its container with no
credential: it reads a secret, spawns another function, and the spawned task
records it as parent and root. Secrets reach workloads as environment
variables and never appear in task output. Scheduled functions run on their
cron in UTC, each occurrence admitted once. Hooks, `in_process` threads and
`callback_url` webhooks behave as in the reference.

## Decisions

- Container API. The supervisor serves HTTP/1.1 on
  `/run/lazycloud-api/api.sock` (env `LAZYCLOUD_CONTAINER_API`), created in a
  tmpfs the agent mounts there. Each request is one `ContainerLink.API`
  stream to the agent and one `HostService.ContainerAPI` stream to the
  server, which serves it with the public `internal/api` handler
  (`api.NewContainerHandler`) under an `identity.ContainerPrincipal`. The
  server checks per request that the container is assigned to the calling
  host and not stopped. The principal reaches only the container's
  workspace. The SDK sends `LazyCloud-Task: <current_task_id()>`; the server
  accepts it only while that task's current attempt runs on the container,
  and spawned tasks take parent and root from the principal, not the body.
  The agent names the container itself and enforces 32 MiB bodies and 64
  calls in flight, since the supervisor is inside the container; the
  supervisor queues callers beyond 32. Authorization headers are dropped.
  The agent also sets `CONTAINER_ID` and `LAZYCLOUD_WORKSPACE`, so
  `is_remote()` and the default workspace work in containers.
- Secrets live in `internal/secrets`. Each write seals the value with
  AES-256-GCM under a fresh data key, bound to workspace and name as
  associated data; the data key is wrapped by a master key outside
  PostgreSQL behind `KeyWrapper` (`FileKey`, 32 random bytes in
  `LAZYCLOUD_SECRETS_KEY_FILE`, locally; KMS in production). StartContainer
  carries exactly the secrets the release names (field 10) and the
  workspace (11). A missing secret stops the container as `start_failed`
  with `secret not found: NAME`, through `Execution.containerExited`. The
  supervisor replaces values of 4 bytes or more with `********` in output
  (held back across chunk boundaries) and in failure messages and
  tracebacks.
- Schedules live in `internal/schedules`, not control: the cron expression
  is part of the release spec control owns, but when an occurrence is due
  and its identity is cron's own decision (update.md). Deploy writes the
  firing row through `schedules.Apply` in the deploy transaction. The
  scheduler claims due rows `FOR UPDATE OF s, w SKIP LOCKED` in batches of
  100, which also skips a workload a deploy holds, and admits each under a
  savepoint through `Execution.SubmitInTx` with `scheduled_for`; `tasks
  (workload_id, scheduled_for)` is unique. The next occurrence is computed
  from now, so missed runs collapse to one. The parser matches croniter
  6.2.4 (the reference's library) on 72 recorded cases.
- Callbacks: every retry or terminal transition (`finishAttempt`,
  `CancelTask`, `containerExited`, `stopRelease`) inserts a
  `task_callbacks` row in its transaction. `internal/notifications`
  delivers due rows from cmd/scheduler: a lease fenced by the delivery
  count, at most 16 in flight, no I/O under a lock, connections dialed only
  to addresses checked as public at dial time, 3 attempts, rows purged a
  week after they finish.
- Hooks are references in the release spec, sent through StartContainer
  (`FunctionWorkload.hooks`), Configure and the runner's `load` frame. The
  runner calls them; it decides `retry_scheduled` as the server does
  (`attempt_number < max_attempts`), so on_retry/on_failure need no round
  trip.
- `in_process` with `concurrency` above 1 runs one runner process with a
  thread per slot. The runner sends each attempt's output as `output`
  frames, and the supervisor's shared process dispatches outcomes by
  attempt id.
- `keep_warm=-1` resolves as in the reference: allowed only with a warm
  floor, forced when `min_containers > 0`, and it means the planner stops
  idle containers above the minimum at once. Scheduled functions default
  to 0.

## Progress

- [x] Contracts: migration 0004, protobuf, OpenAPI (secrets, schedules,
  FunctionSpec runtime fields, Function.schedule, Task lineage), runner
  protocol (hooks, concurrency, lineage, `output` frame)
- [x] Server: container principal, lineage, start failure, callback outbox,
  secrets, schedules, API handlers, host session ContainerAPI
- [x] Host runtime: agent relay, environment, tmpfs; supervisor socket
  server, redaction, shared in-process runner
- [x] Python: runner hooks and threads; SDK container transport; `Secret`
  and `lazycloud secret` on the new API; cron, secrets, callback_url,
  in_process, hooks and keep_warm=-1 removed from
  `Function.unsupported_options()`
- [x] End-to-end acceptance on Docker with the real server

## Evidence

Acceptance, `python/tests/acceptance/test_workload_runtime.py`, run against
server, scheduler and agent on Docker (runc), PostgreSQL 18 and Garage on a
private stack (ports 44xxx): one function reads a secret from its
environment and through `Secret.get()`, calls `child.remote()` and
`child.spawn()`, and fails to reach another workspace; the children record
parent and root; task logs show `token is ********`; hooks log in order;
two `in_process` attempts overlap in one container with their own output;
a retried failure runs on_error/on_retry then on_error/on_failure; signed
callbacks verify with the revealed signing key; an `every 1m` schedule
fires and records its run. Passed in 14.5 s.

Owner tests (PostgreSQL, real gRPC, real runner processes):
`TestParseCronMatchesCroniter`, `TestSecretLifecycle`,
`TestValuesAreSealedAndBoundToTheirName`, `TestListPages`,
`TestSigningKeyIsCreatedOnce`, `TestDeployNormalizesAndReplacesTheSchedule`,
`TestMissedOccurrencesCollapseIntoOneTask`,
`TestConcurrentSchedulersAdmitAnOccurrenceOnce`,
`TestRejectedOccurrencesAdvanceWithTheirReason`,
`TestCallbackIsSignedAndRetriedUntilDelivered`,
`TestCallbackGivesUpOnRejectionAndAfterThreeAttempts`,
`TestCallbacksNeverReachPrivateAddresses`,
`TestContainerPrincipalNeedsTheAssignedHostAndARunningTask`,
`TestSpawnedTasksRecordTheirLineage`,
`TestTransitionsRecordCallbacksInTheirTransaction`,
`TestStartFailedStopsOnlyAStartingContainerOfTheHost`,
`TestContainerAPIReachesOnlyTheContainersWorkspace`,
`TestSpawnFromARunningTaskRecordsItAsParent`,
`TestStartCarriesNamedSecretsAndFailsWithoutThem`,
`TestRedactorHoldsBackAValueSplitAcrossChunks`,
`TestSupervisorRedactsSecretsFromOutputAndFailures`,
`TestInProcessSlotsShareOneRunnerAndCancelRestartsIt`,
`TestContainerAPISocketRelaysRequestsToTheAgent`; runner
`test_runtime_features.py`; SDK `test_sdk_secret.py`,
`test_deploy_maps_workload_runtime_options`.

Measurements (one host, 24 CPUs, Docker 29, same stack):

| Scenario | Result |
| --- | --- |
| `Secret.get()` from a container (socket, supervisor, agent, server, PostgreSQL), 200 calls | p50 1.4 ms, p95 1.9 ms |
| Warm `noop.remote()` from inside a container, 200 calls | p50 8.5 ms, p95 11.2 ms |
| Parent that reads a secret and calls a cold child, end to end | 2.0 s |
| Two 2 s `in_process` attempts | 2.9 s together, cold start included |
| Schedule occurrence due to task admitted | 0.19 s (1 s tick) |
| Idle cost of schedules and callbacks | 2 indexed queries per second per scheduler |

## Intentional differences from the reference

- Secret names are environment variable names (`[A-Za-z_][A-Za-z0-9_]*`,
  at most 240), because workloads receive them as variables; the
  `LAZYCLOUD_` prefix is reserved. Values are at most 64 KiB. The reference
  validated neither. Secret records drop `id` (it was the name),
  `last_updated_by` and `workloads`.
- Revealing a value is its own operation, `getSecretValue`; `secret show`
  without `--reveal` never transfers the value.
- Callbacks are durable: an outbox row commits with the transition. The
  reference posted after commit and lost the callback if the process died.
  Statuses follow the public API: `retry`, `succeeded`, `failed`,
  `cancelled` (the reference's `complete`; a timeout is `failed` with
  `error.kind` `timeout`). The signing key is the workspace secret
  `LAZYCLOUD_CALLBACK_SIGNING_KEY`, which users reveal to verify and set to
  rotate; the reference's key could not be read. Retries land on the
  scheduler's 1 s tick rather than after 0.25 s and 0.75 s.
- An occurrence is admitted at most once (unique `(workload, scheduled_for)`
  in the admitting transaction); the reference could run one twice. Deploy
  rejects an expression that never fires, such as `0 0 30 2 *`, which the
  reference accepted and then failed on every tick. Redeploying an
  unchanged cron keeps the next and last run. A paused app or stopped
  function records each skipped occurrence with its reason, instead of
  firing once on resume.
- Hooks run before the outcome is reported, so their output is in the task
  log and the attempt's timeout covers them; the reference ran on_success
  and on_finish after the result was acknowledged.
- Cancelling an `in_process` attempt kills the shared runner; the other
  attempts in it fail as `WorkerCrashed` and their retry policy applies.
  The reference stopped the whole container.
- A container cannot call `/v1/me` (403): it acts for a workspace, not a
  user.

## Remaining gaps

- KMS: only the `KeyWrapper` interface and the key file exist; a KMS
  implementation and rotation across master keys need AWS credentials and
  are unverified.
- Read-only token scope: `getSecretValue` is separate so the identity
  packet's scopes can deny it; tokens carry no scope yet, so every token
  that reaches the workspace can reveal.
- Dashboard (web packet): Storage → Secrets and the workload Schedule,
  Timezone, Last run and Next run use `listSecrets`, `setSecret`,
  `deleteSecret`, `getSecretValue`, `listSchedules` and `Function.schedule`.
- `InvalidInputError` is retried here; the reference did not retry it.
- Hook contexts leave `stub_id`, `app_id` and `workspace_id` empty.

## Try it

`deploy/local/run.sh start` creates `.lazycloud/secrets.key` and prints the
SDK environment; export it. Then `lazycloud secret create E2E_TOKEN s3cr3t`,
`bin/server admin create-workspace --name other --owner-email
dev@lazycloud.local`, and run the acceptance test with
`LAZYCLOUD_TEST_ENDPOINT`, `LAZYCLOUD_TEST_TOKEN` and
`LAZYCLOUD_TEST_WORKSPACE` set to the exported values:
`uv run --group dev pytest -x -s python/tests/acceptance`. The container
must fail to reach `other` even though the dev user is an administrator.
