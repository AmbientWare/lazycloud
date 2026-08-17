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
- **Retrying only on named exception types.** `RetryPolicy.retry_for` was carried
  and persisted for a while after the runner that read it was gone. A function
  retries on status, so the field was deleted rather than enforced.

The claim is the fence. A container takes work by setting `container_id` on the
`tasks` row under `FOR UPDATE SKIP LOCKED`, and that durable row is the only
record of who owns an invocation — there is no lock beside it. Anything that
stops a container has to release the claim, and a reaper sweeps the ones that
did not.

A claim is only ever written by a container that is still live. One written
afterwards names a container every settlement path has already run past, so
nothing gives it back and its caller waits forever; a start that arrives from a
terminal container is refused instead.

## Cancelling reaches the work, and stops there

Nothing inside a container watches the task row, so writing `cancelled` on it
stops nothing: the handler runs to completion, keeps its side effects, and keeps
being billed while the caller has been told it stopped. Cancelling a function
therefore stops the container it is running in, which is the only lever the
platform has on a running handler.

That container is usually also serving invocations nobody cancelled. They are
released rather than cancelled — the reason is `Scheduler`, because the platform
stopped the container, not the caller who owns those calls. Each one is claimed
again and runs elsewhere, so what is lost is partial execution, never the
invocation: a non-idempotent handler among them re-executes from the start.
Cancelling one call of a concurrent function is that expensive, and the
alternative was a cancel that did not cancel.

Per-invocation cancellation is what would make it cheap, and it is reachable —
outside `in_process`, every invocation already has its own process. It needs a
control-plane-to-container signal that does not exist yet.
