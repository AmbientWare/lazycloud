# Scheduler package

Queueing, assignment, autoscaling, capacity decisions, worker and fleet hot
state, route publication, retries, and cancellation, behind typed Redis
repositories.

Durable history belongs to explicit database owners. Endpoint, pod, gateway,
worker, and process composition belongs to apps.

Everything here is concurrent by nature. Leases, assignment, retries, and
terminal cancellation have to hold when two schedulers race, when a worker dies
mid-task, and when a lease expires under work that is still running. Prefer a
design where losing a race is safe over one where losing it is merely unlikely.

## One autoscaler, three workloads

`AutoscalingDriver` runs the reconcile pass and a `WorkloadAutoscaler` supplies
what is genuinely per kind: which stubs it selects, the signal it samples, the
count that signal argues for, how it starts one container, and which containers
it may stop. A function scales on backlog depth, an endpoint on in-flight
dispatches, a pod on connections. That difference is the strategy, and nothing
above it is.

This was three separate services, on the argument that a spine over three
unrelated samples would hide the per-kind content and that the safety could be
shared by being the same code rather than the same abstraction. Written out
three times it stopped being the same code. The function autoscaler, the third
copy, never read the pause flag an operator sets, never recorded a metric,
never emitted an event the history endpoint could return, and wrote a state row
that said no actions were taken however many it took. Each omission was
invisible in the diff that made it, because the copy it was missing from was
complete on its own terms.

So the safety is now unreachable from a workload rather than repeated in each:
stub selection including the pause, the stub lock, the contention metric, the
failed-container threshold and its window, the inactive deployment, the
workspace guardrail, and the metrics, event, and state row a lock-holding tick
leaves behind. A workload cannot skip one of those, because it is never handed
them.

Composition, not inheritance. A base class with abstract hooks would reuse the
same code, but it would also put the pass in the subclass's reach, which is the
door the three copies walked through. A strategy is handed a stub, a signal, and
a count, and hands back a plan.

The kinds differ in what they scale on, not in what an operator can ask about
them. One `AutoscaleResult` carries `kind`, `signal_name` and `signal_value`
instead of a field per kind, so the state row, the metrics, the history, and the
reconcile output have one shape and no per-kind branch to forget.

A scale-down is the platform's decision, so it stops containers with
`StopContainerReason.Scheduler`. The `User` default would settle the claims a
container holds as cancellations, which tells callers their work was cancelled
when what happened is that capacity moved.

Capacity for a function is owned here. An invocation may start the first
container for an idle stub so a cold call does not wait for a tick, and nothing
else may start one. The ceiling is enforced where the container is reserved, in
the transaction that both counts what is live and inserts the row that adds to
it.

A fired schedule invokes the stub its deployment published, read off the
deployment the tick just resolved rather than off anything the schedule row
carries: the deployment is what a redeploy updates, so it is the only one of the
two that cannot be stale.

## A record the scheduler no longer backs

Capacity acquisition has a retry deadline separate from dispatch readiness.
Pending requests keep checking usable workers every dispatch interval while
`capacity_retry_at` prevents another purchase attempt before its cooldown ends.
A worker returning during that cooldown can accept work immediately.

Capacity is counted from the durable container rows, so a row that says
`pending` or `running` while nothing is going to make it true is a ceiling slot
held against a workload that cannot use it. At `max_containers = 1` that is not
a degradation but a stop: desired equals current on every tick, and a scheduled
function grows its backlog by one task per fire with nothing able to start.

Which rows count is not a per-kind question, so the driver answers it and no
workload can. It was a per-kind question once, and two of the three kinds
answered it with "all of them". The pod autoscaler dropped running records the
scheduler had lost, and functions and endpoints classified nothing at all.

A `running` row with no scheduler state has started, so nothing is coming back
for it. A `pending` row has two legitimate reasons to still be pending, and both
are read rather than guessed at. A request still queued for it is the
dispatcher's, which bounds its own retrying and fails the request itself; that
is read directly, so no clock has to allow for it. Otherwise a worker holds it
and is starting it, and `CONTAINER_START_DEADLINE_SECONDS` bounds that and only
that.

Not the Redis TTL, which was the recovery before this and is the wrong owner
twice over: the durable row stayed wrong until a cache key lapsed, and the key
is re-armed by whoever holds the container, so a worker wedged part-way through
a start refreshes it for as long as it stays up. The deadline is wall-clock on
the durable row, which is created first and outlives every cache entry about it.

Reclaiming stops the container with `StopContainerReason.Scheduler`, the same
settlement every platform-owned stop takes, so an invocation the container had
claimed is released back to the queue rather than reported to its caller as
cancelled. The stub lock serializes ticks, and where two overlap anyway the
second finds the row already terminal and settles nothing twice.

## A function's warm floor

`min_containers` is held with nothing queued, and it is the only way to ask this
platform for interpreters that are already warm: a model resident in VRAM, a
handler already imported. `@app.function` is the one task execution model here,
so a user who needs that has nowhere else to express it. A platform with several
task-driven kinds can refuse a floor on one of them and let another carry the
warm capacity; with one kind there is nowhere else for it to go.

A floor and a finite idle window contradict each other: a function container
retires itself when the window passes with no work, so the floor would start,
idle out, and start again on the next tick, a count that is right whenever it
is read and warm at no point. Declaring a floor therefore makes the window
infinite, which is the same coupling pods take from the other end, and it moves
removal to the autoscaler because nothing else will do it.

Scale-down stops only containers holding no invocation. That is what makes a
drain unnecessary rather than deferred: a stop settles claims by releasing them,
so no invocation would be lost, but the part of one that had already run would
be, and a handler that is not idempotent would run it twice. Skipping busy
containers means the count stays above the floor while work is in flight, which
is the honest answer. Those containers are doing the thing they exist for.

Where there is a window, scale-down does nothing and self-retirement removes the
excess. Stopping a container early there would throw away the warm container the
next call was about to reach, which is the whole point of pooling.

The floor is held per stub and every deployed version keeps its own, so a
redeploy releases the floor the version before it held. An author asking for two
resident interpreters wants two, not two more each time they ship, and prior
versions stay invocable by number. Nothing else would ever remove those
containers, since a floor is what makes their window infinite. The release sets
the floor to zero and leaves the window infinite, which reads backwards and is
the only thing that works: a container took its keep-warm seconds from the
environment it started with, so a finite window written to the config would
reach the config and not them. A zero floor with no window is what hands them to
scale-down.

Every function stub is selected, bound to a deployment or not. A stub reached
through `.remote()`, `.map()` or `lazycloud run` before anything is deployed has
a backlog like any other, and it is the one case where the first container came
from an invocation rather than from here, so refusing it leaves a fan-out being
served one container at a time.

## Priority ranks capacity, and higher wins

One number, one direction, in both places that read it: the order capacity is
acquired in and the choice of which existing worker a request lands on. It was
briefly opposite in two sorts ten lines apart, one ascending and one negated,
which is the shape of a value whose polarity nobody wrote down.

It is a tier, not a weight. Work fills the highest tier that fits before any of
the next, and inside a tier placement is exactly what it was. A weighted score
would make preference and free CPU commensurable, which needs a ratio nobody can
justify, and it would put the packing that lets an idle pool drain at the mercy
of whatever number an operator typed. A tier cannot be tuned into breaking it.

Below liveness, though. A worker that has not registered yet is not a better
choice than one that can run the request now, however it is ranked.

The unit owns it and the worker is told, like tenancy and billing owner beside
it. A machine its customer holds root on could otherwise register the largest
integer there is and pull every one of that account's requests onto itself,
starving the cloud pool that account is paying for.

It is a property of capacity rather than of work, so there is no per-request
priority. Requests are served oldest first.
