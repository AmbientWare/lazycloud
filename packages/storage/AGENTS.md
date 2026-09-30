# Storage

- Own protocol-backed object/mounted storage and durable resource records.
  Price metering in the transaction that records it.
- Validate paths, hashes and multipart state. Scope credentials/URLs narrowly,
  preserve sibling objects on deletion and recover partial operations.
- A workspace disk has one holder. Fence publication with its lease and retain it
  through final publish/release or proven worker loss. Delete only its exact prefix.
- Collect old chunks only after a self-contained generation is durable. Local
  heat maps are hints, not authority or billable data.
- Provider volume calls run outside transactions; claim each call and fence results
  by revision/lease. Record completed calls even after lease changes. Recover dead
  drivers within acquisition deadlines; observation alone is not a claim.
- Persist deletion intent, then let housekeeping remove provider volumes/objects
  before rows. Retain orphan creation records and connection ownership until exact
  cleanup; tag-scoped collection deletes only detached, unreferenced owned volumes.
- Volume deletion ends metering but retains ownership until cleanup. Admission,
  writes and deletion use the same row lock. Complete presigned multipart writes
  through the API; retain exact-ID cleanup for previously issued direct writes.
- Buckets remain with their workspace's platform or connected account. Connected
  storage uses short-lived bucket-scoped sessions, renewed before expiry.
- Exhausted credit grants 30 days of uncharged managed-storage retention. Queue
  notification atomically; replenishment closes retention before deletion claim.
- Claim expired objects under the credit lock without blocking on resource locks.
  Reuse normal cleanup/retries; never delete whole workspaces/buckets or BYO data
  merely because credit ran out.
