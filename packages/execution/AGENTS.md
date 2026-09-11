# Execution package

User execution resources: task, artifact, volume, and secret workflows, the
deterministic planners behind them, and the collections and signals they
coordinate through.

Planners stay pure: same inputs, same plan, no I/O. What they decide can then be
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
record of who owns an invocation. There is no lock beside it. Anything that
stops a container has to release the claim, and a reaper sweeps the ones that
did not.

A claim is only ever written by a container that is still live. One written
afterwards names a container every settlement path has already run past, so
nothing gives it back and its caller waits forever; a start that arrives from a
terminal container is refused instead. So the order of a stop matters: the
container's terminal status is written before its claims are settled, or the
container being stopped takes back what it just gave up and then goes away
holding it. Where a settlement path can write both in one transaction it does;
the stop cannot, so it orders them.

An idle function closes work admission before its runner exits. Retirement holds
the stub capacity lock and the container claim fence while checking runnable and
in-flight work. The drain record uses a zero serving floor, so it reserves no
replacement until another invocation needs one. The container remains physically
allocated and billable until the worker observes its exit. Task attempts retain
the last completion time even after retries release or move a task's claim.

Who ended the container decides whether its work is charged for the attempt. The
platform stopping one, whether scaling down, draining, or cancelling a neighbour,
costs the invocation nothing: it is claimed again with its budget intact, and the
caller sees only that it ran somewhere else. A container that died on its own
having failed is the one exit the work itself may have caused, and there the
invocation is charged an attempt and retried on its own terms. Handing it back
free is what turns an invocation that kills its interpreter into a loop nothing
bounds, because a claim returning to an attempt still marked running never
advances the counter that `max_attempts` reads.

`StopContainerReason` is how that is decided, and it carries three separate
weights that nothing in its own module shows. Settlement reads it to choose
whether a claim is cancelled or released. The worker reads it to normalize an
exit, where `Unknown` on a SIGTERM means the container died on its own and
scores the run a success. And the customer reads a phrase derived from it. A
reason picked for how it reads therefore moves money, and `Unknown` in
particular is not a way of saying nobody stated one. An unstated stop travels
as `User` and declines to describe itself instead.

That is also why a stop that a container is serving other callers through says
`Scheduler` rather than `User` or `Admin`. Those two cancel, which is terminal
and carries no retry, and the calls a drain or an app stop interrupts belong to
people who asked for none of it.

## Cancelling reaches the work, and stops there

Nothing inside a container watches the task row, so writing `cancelled` on it
stops nothing: the handler runs to completion, keeps its side effects, and keeps
being billed while the caller has been told it stopped. Cancelling a function
therefore stops the container it is running in, which is the only lever the
platform has on a running handler.

That container is usually also serving invocations nobody cancelled. They are
released rather than cancelled. The reason is `Scheduler`, because the platform
stopped the container, not the caller who owns those calls. Each one is claimed
again and runs elsewhere, so what is lost is partial execution, never the
invocation: a non-idempotent handler among them re-executes from the start.
Cancelling one call of a concurrent function is that expensive, and the
alternative was a cancel that did not cancel.

Per-invocation cancellation is what would make it cheap, and it is reachable:
outside `in_process`, every invocation already has its own process. It needs a
control-plane-to-container signal that does not exist yet.

## A schedule is a property, not a kind

`@app.function(cron=...)` is the only way to declare one, and a scheduled
function is a function in every other respect: the same stub kind, the same
invoke path, the same claim, the same retries. What a schedule changes is that
something other than a caller starts the work.

Two consequences follow from that and nothing else does. Its containers keep no
idle window by default, because the next run is usually further away than any
window worth paying for. And a run that has already fired can outlive the
deployment that scheduled it, so it is cancelled when that deployment has been
stopped or deleted. Every other invocation has a caller, and a caller cannot
invoke something that is gone.

There used to be a `CronJob` kind alongside `Function`. Fifteen places had to
remember to name both, one of them an authorization set, and the autoscaler
forgot. A schedule was the one function-shaped workload nothing would provision
for.

The kind was also what made a schedule impossible to attach to anything that
could not run one, so the contracts now say it: only a function may carry
`cron`, refused where the spec is written rather than at the tick. A pod with a
schedule fires against a stub that cannot serve it and records the same failure
every minute for as long as the deployment lives.

A schedule is one per resource, not one per version, and it is answered on every
deploy rather than only on the deploys that declare one. The row is named for
the subdomain that every version shares, so a spec with no `cron` is stating
that this resource has no schedule. Left unsaid, the row a previous version
wrote outlives the source line that asked for it.
