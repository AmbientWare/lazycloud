# Storage package

Protocol-backed object and cache storage, S3-compatible clients, mounted storage,
and the durable records that describe stored objects.

API and CLI formatting, scheduler and worker loops, providers, duplicate
persistence, and local JSON stores stay out.

Volume metering is billable, so a metering window is priced in the transaction
that records it rather than by a later pass. A record committed without its cost
is money this platform measured and can no longer charge for, and the usage row
alone cannot say whether the cost was skipped or never owed.

A sized block device with its own filesystem and a restore point is something
this package does not offer, and that is a decision rather than an oversight.
It is what anything stateful actually wants, and a shared volume mount is not
it. Under bring-your-own capacity the disk sits on the customer's own
infrastructure and is usually already provisioned, so the gap costs them
nothing they cannot solve where the hardware is. Reach for this note before
concluding that snapshots were simply forgotten.

This package handles user data, so its invariants are the ones whose failure
cannot be undone. Validate paths and keys against traversal, verify checksums,
track multipart state, scope credentials and presigned URLs narrowly and briefly,
keep concurrent writers from corrupting each other, preserve sibling objects on
every delete, and clean up what a failed operation left behind.

Volume deletion records intent before calling storage. That instant ends customer
metering. The volume remains visible and owned until housekeeping finishes cleanup;
container admission and file writes lock the same volume row as deletion.

Presigned volume writes use multipart uploads whose completion goes through the
API. Direct PUT requests can finish after their URL expires, so volumes that may
have issued those URLs retain an unmetered cleanup record for their exact ID.
Recreating a name gets a different ID. Managed bucket retirement removes those
records; customer bucket cleanup retains its canonical workspace storage authority
and remains workspace-owned after public workspace access ends.

An empty or negative credit balance starts a 30-day managed-storage grace
period and queues an email in the same transaction. Its timestamp is durable;
another sweep cannot restart it. Replenishment before a deletion claim closes
the period. Storage charges
inside a grace period are waived through the billing ledger's settlement owner.

After grace, claim individual objects and volume deletion intents under the
same account lock that protects credit issuance. Skip busy resource locks while
holding that lock because metering takes its resource lock before billing.
Existing cleanup owns retries and late-write cleanup. Never retire a workspace,
delete a whole bucket, or reclaim customer-owned storage for exhausted credit.
