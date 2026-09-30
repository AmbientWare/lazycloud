# Scheduler

- Own queueing, assignment, autoscaling and placement through typed repositories.
  Compute owns provider acquisition; apps own wakes, cadence and process lifetime.
- PostgreSQL owns requests, retry generations and assignments; Redis publishes and
  leases them. Recover only unassigned work with its original time and payload.
- Uncertain delivery retains assignment. Clearing requires its token and proof of
  non-delivery; missing Redis state never authorizes another executor or a stop.
  Billing placement cannot be cleared after usage or ledger facts exist.
- Request polling proves intake; keepalive proves only liveness. Use the same
  admission decision for placement/headroom and validate version/lease atomically.
- Delivery/start deadlines use recorded timestamps, independent of heartbeats.
  Recovery shares the container transition lock and respects a completed startup.
  Stop confirmed orphans through the container service to release claims.
- Autoscaling shares one driver and per-workload decisions. Repositories own
  claims/state; avoid services that only forward repository calls.
- Take stub ownership before shared snapshots and revalidate queued leases.
  Manual and scheduled reconciliation share locks and execution. Database
  admission and conditional stops remain authoritative after lease expiry.
- Platform scale-down uses `StopContainerReason.Scheduler`. Enforce function
  ceilings transactionally at reservation; include undeployed function backlogs.
- Warm floors disable idle retirement. Scale down only idle function containers;
  release older deployment floors without interrupting in-flight work.
- Cron invokes the deployment's current published stub, not a stale schedule copy.
- Higher capacity priority wins after liveness. Requests share service across
  admission accounts; retries retain their charge and cannot starve other accounts.
  Fairness snapshots do not establish current placement authority.
- Require matching placement identities supplied by their domain owner. Never
  widen platform/private tenancy or substitute capacity for a named machine.
- Pack compatible resources while preserving large hosts for shapes needing them.
  Consolidation rechecks named destinations under leases and moves only work whose
  durable preemption policy permits it; pinned/non-preemptible work stays.
