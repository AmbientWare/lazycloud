# Workload runtime

Packet: container API, secrets, schedules, lifecycle hooks, `in_process`
slots and task callbacks (tasks/wave-2.md). Migration
`migrations/0004_workload_runtime.sql`; protobuf fields 10-19 in existing
host and container messages.

## Outcome

A deployed function can call the platform from inside its container with no
credential: it spawns another function, reads a secret, and the spawned task
records it as parent. Secrets reach workloads as environment variables and
never appear in task output. Scheduled functions run on their cron in UTC,
each occurrence admitted exactly once. Hooks, `in_process` threads and
`callback_url` webhooks behave as in the reference.

## Decisions

- Container API. The supervisor serves HTTP/1.1 on
  `/run/lazycloud-api/api.sock`, created inside the container on a tmpfs the
  agent mounts there. Each request becomes one `ContainerLink.API` stream to
  the agent and one `HostService.ContainerAPI` stream to the server, which
  serves it with the public `internal/api` handler under a container
  principal. The principal reaches only the container's workspace; the
  `LazyCloud-Task` header names the calling task, which the server accepts
  only while that task's current attempt runs on the container. Spawned tasks
  take their parent and root from the principal, not from the request body.
  Bodies are capped at 32 MiB and in-flight requests per container at 64 by
  the agent, which does not trust the supervisor; the supervisor queues
  callers beyond 32 so user code sees backpressure, not errors. The server
  checks on every request that the container is assigned to the calling host
  and is not stopped.
- Secrets live in `internal/secrets`. Each value is sealed with AES-256-GCM
  under its own data key, bound to its workspace and name as associated
  data, and the data key is wrapped by a master key outside PostgreSQL: a
  local key file in development, KMS behind the `KeyWrapper` interface in
  production. StartContainer carries exactly the secrets the release names;
  a missing one fails the container start with `secret not found: NAME`, as
  in the reference. The supervisor replaces every secret value of four bytes
  or more in task output and failure messages with `********`.
- Schedules live in `internal/schedules`. The cron expression is part of the
  release spec that control owns; the `schedules` row is the firing state
  (next occurrence, last run) that control writes through `schedules.Apply`
  inside the deploy transaction. Cron owns when an occurrence is due and its
  identity, which update.md names as its own decision, so it is not control
  code. The scheduler claims due rows `FOR UPDATE OF s, w SKIP LOCKED` in
  batches of 100 and, per row under a savepoint, admits the task through
  `Execution.SubmitInTx` with `scheduled_for` set; `tasks (workload_id,
  scheduled_for)` is unique. The next occurrence is computed from now, so
  missed runs collapse to one.
- Callbacks: the transition that moves a task to retry or a terminal status
  inserts a `task_callbacks` row in the same transaction. A bounded worker in
  cmd/scheduler delivers due rows with `SKIP LOCKED`, at most three attempts.
  The signing key is the workspace secret `LAZYCLOUD_CALLBACK_SIGNING_KEY`,
  created on first delivery, so users can reveal and rotate it.
- Hooks are carried by reference in the release spec and run by the runner.
  `in_process` runs one runner process with a thread per slot; it reports
  output per attempt as frames because threads share stdout.

## Plan

1. Contracts: migration, protobuf, OpenAPI paths and FunctionSpec fields,
   runner protocol additions. Regenerate.
2. Server: identity container principal, execution container authority,
   lineage, start failure, callback outbox; secrets; schedules; API handlers;
   host session ContainerAPI and secret resolution; wiring.
3. Host: agent relay, environment and tmpfs; supervisor socket server,
   redaction, in-process mode.
4. Python: runner hooks and in-process threads; SDK container transport,
   Secret and `lazycloud secret` on the new API, cron and callback options.
5. Acceptance against PostgreSQL, Docker and the real server.

## Progress

- [ ] Contracts
- [ ] Server
- [ ] Host runtime
- [ ] Python
- [ ] End-to-end acceptance

## Intentional differences from the reference

To be completed with evidence.
