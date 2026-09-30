# Disk engine

- Own local disk operations, state and layers; the control plane owns published
  generations. NBD allocation uses a host-wide lock outside any disk directory.
- Acknowledge publication only after durable control-plane acceptance, idempotently.
  Reuse local state only when generation and manifest digest match.
- Freeze before deciding what to seal; preserve unknown-head writes and record
  a new head before switching the daemon. Recover dead heads crash-consistently.
- Compact only published layers under the disk lock, removing daemon references
  before files. Never compact into a lazy layer or flatten before hydration.
- Collect only unreachable data after a self-contained generation is published;
  retain pending uploads. Stream sparse flattening instead of copying whole disks.
- Check space, including lazy reservations, before destructive work. Disks grow
  but never shrink. Preserve exit codes: 3 space, 2 usage, 1 failure.
- Verify fetched chunk sizes/hashes; flush data before its availability bitmap.
  Fetch and space failures surface as I/O errors and observable serve failures.
- Refresh storage credentials until detach; fail when expired. Prioritize workload
  reads over hydration; heat maps influence ordering, never data authority.
- Stop serving before recovery or eviction; bound fetch cancellation. Missing
  tools, modules or manifests fail explicitly.
