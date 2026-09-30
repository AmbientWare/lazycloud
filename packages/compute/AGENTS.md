# Compute

- Own provider-neutral capacity, offers, units, machine lifecycle and admission.
  Keep adapters, scheduler/worker loops and app composition outside this package.
- Resolve capacity by stable provider/placement identity. Preserve customer-owned
  paths and cleanup even when an offer or provider no longer permits purchases.
- Keep credentials out of images, user-data and launch templates. Verify signed
  instance identity; encrypt authenticators and distinguish them from nonces.
- Fleet growth/replacement reads commitments and mutates under the fleet
  transaction lock. Retiring or failed capacity retains commitments until
  provider absence and storage cleanup are observed.
- Maintenance operations durably own their source and replacement exclusively.
  Recheck placement, intake, storage and allocations before draining; interruption
  recovery takes precedence. Idle in-place updates require no live allocations.
- A stopped reserve is usable only after release/driver preparation is proven.
  Keep requested sleep, accepted sleep, observed stop, saved memory and restored
  memory as separate evidence. Telemetry never authorizes admission.
- Returning used hosts requires durable drain, stopped workloads/workers and a
  receipt fenced to the stop and cache generation. Clear tenant data and worker
  credentials while preserving machine identity.
- Forecast compatible shapes, not aggregate resources that cannot fit one host.
  Pending launches cannot justify retiring ready capacity. Expired/missing plans
  cannot authorize discretionary retirement or borrowing another market's reserve.
- Persist launch intent before provider calls and fence inventory writes by
  revision. Cancel persistent Spot requests before terminating their instances.
  Preserve exact resource ownership through retries and partial failure.
- Advance machine phases only through the lifecycle owner; publish after commit.
  Heartbeats do not reset phase deadlines. Disconnection is not a lifecycle phase.
  Placement eligibility must not revoke a draining machine's existing runtime.
- Connected accounts and joined machines belong to users. Resolve workspace
  ownership authoritatively; connection-wide changes include all owned workspaces.
  A machine's explicit served-workspace list and placement identity govern access.
- Stamp tenancy from verified credentials, never host claims; reconcile stale
  stamps. Preserve account isolation at admission, credentials and network gates.
- Deployments pin placement; other requests resolve it at request time. Stubs
  carry no placement. Named machines never fall back to another capacity source.
- Record immutable supplier quotes without confusing estimates with invoiced
  charges. Unknown complete costs refuse new platform purchases; customer-owned
  infrastructure is outside platform margin policy.
- Deletion requires durable intent. A deleting unit cannot reactivate; revival of
  a deleted unit uses a new generation. Retirement rechecks demand, reservations,
  leases and storage evidence while retaining history; customer pools do not expire.
- Storage absence evidence uses the provider observation time and matching cache
  generation, not reconciliation start time.
