# Execution Package

User execution resources: task, artifact, volume, and secret workflows, the
deterministic planners behind them, and the collections and signals they
coordinate through.

Planners stay pure—same inputs, same plan, no I/O—so what they decide can be
reasoned about without running it. Services take explicit protocols and explicit
arguments and raise typed domain errors.

Concrete scheduler, worker, gateway, provider, app, and SDK implementations stay
outside. Persistence, authorization, retries, and cleanup are invariants of this
package: a task that fails still has to leave the workspace consistent and its
resources released.

## One task execution model

A function is the only task-driven workload. `@app.task_queue` existed largely
to supply the pooling functions lacked, and was strictly weaker while doing it:
JSON-only results against cloudpickle, no dependency graph, a fixed pool of
consumers per container. It was deleted once functions pooled, ran several
invocations at once, refused a fan-out they could not get to, and scaled to
their backlog.

Three of its capabilities were deliberately not carried over, and each is a
decision rather than an oversight:

- **A consumer-capacity report.** A task queue could say how many consumers were
  live and how many were busy, because its consumers were a fixed pool. A
  function's slots are not, so the equivalent number would not mean the same
  thing. Depth is answered by the unclaimed task count the autoscaler reads;
  everything else is answered by listing tasks and containers.
- **A queue-message TTL sweep.** It expired messages whose deadline passed while
  the task row was still pending, which only matters where a second durable
  store can hold work the row does not reflect. A function has one store. The
  stall it guarded against is covered before the fact by `max_pending_tasks` and
  after it by the sweep that gives claimable work somewhere to run.
- **Retrying only on named exception types.** `RetryPolicy.retry_for` is still
  carried and persisted, but only the task queue runner ever read it. A function
  retries on status. Setting it changes nothing today; enforcing it in the
  function runner is the open question, not deleting the field.

The claim is the fence. A container takes work by setting `container_id` on the
`tasks` row under `FOR UPDATE SKIP LOCKED`, and that durable row is the only
record of who owns an invocation — there is no lock beside it. Anything that
stops a container has to release the claim, and a reaper sweeps the ones that
did not.
