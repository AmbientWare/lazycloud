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
