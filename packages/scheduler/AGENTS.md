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

## Autoscalers stay separate

There are three — function, endpoint, pod — and they are written as three
rather than one spine with three configurations. They share the shape of a
reconcile pass (select stubs, take the stub's lock, decide, act) and nothing
below it: a function scales on backlog depth, an endpoint on in-flight
dispatches, a pod on connections, and each produces a different result
contract. A generic spine over three unrelated samples and three unrelated
results hides the per-kind differences that are the whole content.

What they must share is the safety, and that is shared by being the same code
rather than the same abstraction: every one of them bounds starts by the failed
container threshold within a window, applies the workspace resource guardrail
before scaling up, and records its state even on a tick that found the lock
held. A tick with no state recorded reads as an autoscaler that never looked.

Capacity for a function is owned here. An invocation may start the first
container for an idle stub so a cold call does not wait for a tick, and nothing
else may start one — the ceiling is enforced where the container is reserved,
in the transaction that both counts what is live and inserts the row that adds
to it.
