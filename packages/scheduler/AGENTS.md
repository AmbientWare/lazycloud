# Scheduler Package

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
dispatches, a pod on connections — that difference is the strategy, and nothing
above it is.

This was three separate services, on the argument that a spine over three
unrelated samples would hide the per-kind content and that the safety could be
shared by being the same code rather than the same abstraction. Written out
three times it stopped being the same code. The function autoscaler — the third
copy — never read the pause flag an operator sets, never recorded a metric,
never emitted an event the history endpoint could return, and wrote a state row
that said no actions were taken however many it took. Each omission was
invisible in the diff that made it, because the copy it was missing from was
complete on its own terms.

So the safety is now unreachable from a workload rather than repeated in each:
stub selection including the pause, the stub lock and the state a contended tick
still records, the failed-container threshold and its window, the inactive
deployment, the workspace guardrail, and the metrics, event, and state row a
tick leaves behind. A workload cannot skip one of those, because it is never
handed them.

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
else may start one — the ceiling is enforced where the container is reserved,
in the transaction that both counts what is live and inserts the row that adds
to it.
